/// The transport (scaffold defects D5, D6, D7).
///
/// What this client deliberately does NOT do:
///
/// * **No transparent refresh-on-401 retry.** `docs/member-auth.md` is
///   explicit: any 401 ends the session. The scaffold looped — refresh, retry,
///   swallow the error with `catch (_)` — which both re-POSTs mutations and
///   hides family revocation from the user. Proactive refresh happens in the
///   session layer BEFORE a request is made; a 401 that still arrives means
///   the credential is dead, and the only correct response is to discard both
///   tokens and return to login.
/// * **No unconditional `jsonDecode`.** 204s, empty bodies and HTML error
///   pages from an intermediary are all real; each surfaces as a typed
///   [ApiError] rather than a decode crash on top of the original failure.
/// * **No optional idempotency.** Every mutation requires a key. A caller that
///   forgets one is a programming error, raised as such, not a silent hole in
///   the FM-G guarantee.
library;

import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import '../models/api_error.dart';
import 'certificate_pinning.dart';

/// Supplies the in-memory access token, or null when there is no live session.
typedef AccessTokenProvider = String? Function();

/// Called when the server ends the session (any 401). The session layer
/// listens and tears down: discard tokens, purge cache, route to login.
typedef SessionEndedCallback = void Function();

/// Guards which paths this client is allowed to name. The member app installs
/// a `/member` prefix guard so that a staff path cannot be requested even by
/// mistake — the client half of FM-H, paired with the CI literal sweep and the
/// server's own 403.
typedef PathGuard = bool Function(String path);

class GpHttpClient {
  GpHttpClient({
    required Uri baseUrl,
    required String tenantId,
    required AccessTokenProvider accessToken,
    CertificatePinning? pinning,
    SessionEndedCallback? onSessionEnded,
    PathGuard? pathGuard,
    http.Client? inner,
  })  : _baseUrl = baseUrl,
        _tenantId = tenantId,
        _accessToken = accessToken,
        _onSessionEnded = onSessionEnded,
        _pathGuard = pathGuard,
        _inner = inner ?? _pinnedClient(pinning) {
    if (baseUrl.scheme != 'https') {
      // FM-F: cleartext is refused at construction, not per request, so no
      // build can ever be configured into an unencrypted transport.
      throw ArgumentError.value(
          baseUrl, 'baseUrl', 'Only https:// is permitted');
    }
  }

  final Uri _baseUrl;
  final String _tenantId;
  final AccessTokenProvider _accessToken;
  final SessionEndedCallback? _onSessionEnded;
  final PathGuard? _pathGuard;
  final http.Client _inner;

  static http.Client _pinnedClient(CertificatePinning? pinning) =>
      IOClient(pinning?.buildClient() ?? HttpClient(), pinning);

  Future<Map<String, Object?>> get(String path,
      {Map<String, String>? query}) async {
    final http.Response response = await _send(
      (Uri uri) => _inner.get(uri, headers: _headers()),
      path,
      query,
    );
    return _decodeObject(response);
  }

  /// [idempotencyKey] is required, not optional — see the library doc.
  Future<Map<String, Object?>> post(
    String path, {
    required Object? body,
    required String idempotencyKey,
    Map<String, String>? query,
  }) async {
    final String encoded = jsonEncode(body ?? const <String, Object?>{});
    final http.Response response = await _send(
      (Uri uri) => _inner.post(
        uri,
        headers: _headers(idempotencyKey: idempotencyKey, json: true),
        body: encoded,
      ),
      path,
      query,
    );
    return _decodeObject(response);
  }

  Future<http.Response> _send(
    Future<http.Response> Function(Uri) call,
    String path,
    Map<String, String>? query,
  ) async {
    final PathGuard? guard = _pathGuard;
    if (guard != null && !guard(path)) {
      throw ArgumentError.value(
          path, 'path', 'Path is outside this client\'s allowed surface');
    }
    final Uri uri = _baseUrl.replace(
      path: '${_baseUrl.path}$path',
      queryParameters: query,
    );
    final http.Response response;
    try {
      response = await call(uri);
    } on Object {
      // Pin rejections, DNS failures and dropped sockets are all one thing to
      // the UI: the request never happened. The underlying exception is not
      // propagated — its message can carry host and certificate detail that
      // has no place in a crash report (FM-A).
      throw const ApiError(kind: ApiFailureKind.transport, statusCode: null);
    }
    if (response.statusCode == 401) {
      _onSessionEnded?.call();
      throw ApiError.fromResponse(response.statusCode, response.body);
    }
    if (response.statusCode >= 400) {
      throw ApiError.fromResponse(response.statusCode, response.body);
    }
    return response;
  }

  Map<String, String> _headers({String? idempotencyKey, bool json = false}) {
    final String? token = _accessToken();
    return <String, String>{
      // D6: required on the pre-auth routes so the server can resolve the
      // tenant before any credential exists, and harmless afterwards. Baked
      // per flavor — one white-label build per SACCO, never a runtime switch.
      'x-tenant-id': _tenantId,
      'accept': 'application/json',
      if (json) 'content-type': 'application/json',
      if (token != null) 'authorization': 'Bearer $token',
      if (idempotencyKey != null) 'Idempotency-Key': idempotencyKey,
    };
  }

  Map<String, Object?> _decodeObject(http.Response response) {
    if (response.statusCode == 204 || response.bodyBytes.isEmpty) {
      throw ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: response.statusCode,
      );
    }
    final Object? decoded;
    try {
      decoded = jsonDecode(utf8.decode(response.bodyBytes));
    } on FormatException {
      throw ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: response.statusCode,
      );
    }
    if (decoded is! Map<String, Object?>) {
      throw ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: response.statusCode,
      );
    }
    return decoded;
  }

  void close() => _inner.close();
}

/// Minimal `dart:io` bridge for `package:http` — inlined rather than pulling
/// `http/io_client.dart`'s public surface, so the pinned [HttpClient] is the
/// only path to the network.
class IOClient extends http.BaseClient {
  IOClient(this._io, [this._pinning]);

  final HttpClient _io;
  final CertificatePinning? _pinning;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final HttpClientRequest ioRequest =
        await _io.openUrl(request.method, request.url);
    request.headers.forEach(ioRequest.headers.set);
    final List<int> body = await request.finalize().toBytes();
    if (body.isNotEmpty) {
      ioRequest.add(body);
    }
    final HttpClientResponse ioResponse = await ioRequest.close();
    // The SPKI half of the pin check. Under enforce this is a second gate
    // behind the trust-anchor restriction that already governed the handshake;
    // under report it is the staleness signal.
    _pinning?.verifyPeer(ioResponse.certificate);
    final Map<String, String> headers = <String, String>{};
    ioResponse.headers.forEach((String name, List<String> values) {
      headers[name] = values.join(', ');
    });
    return http.StreamedResponse(
      ioResponse,
      ioResponse.statusCode,
      contentLength:
          ioResponse.contentLength == -1 ? null : ioResponse.contentLength,
      headers: headers,
      reasonPhrase: ioResponse.reasonPhrase,
    );
  }

  @override
  void close() => _io.close(force: true);
}
