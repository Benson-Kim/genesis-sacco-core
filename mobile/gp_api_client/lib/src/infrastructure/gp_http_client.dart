import 'dart:convert';
import 'dart:io';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import '../models/api_error.dart';
import 'certificate_pinning.dart';
import 'token_storage.dart';

/// HTTP client wrapper for the Genesis Prestige API.
///
/// Responsibilities:
/// - Attaches Bearer token from [TokenStorage] to every request.
/// - Performs transparent token refresh on 401 (single retry).
/// - Enforces certificate pinning via [CertificatePinning].
/// - Surfaces errors as [ApiError] — never raw stack traces (§1.6).
/// - Forwards [Idempotency-Key] header when provided (§1.4).
class GpHttpClient {
  GpHttpClient({
    required this.baseUrl,
    required this.tokenStorage,
    required this.pinning,
    http.Client? httpClient,
    this.onTokensRefreshed,
  }) : _http = httpClient ?? IOClient(pinning.buildHttpClient());

  final String baseUrl;
  final TokenStorage tokenStorage;
  final CertificatePinning pinning;
  final http.Client _http;

  /// Called after a successful silent token refresh so callers can persist
  /// the new pair (e.g. Riverpod provider invalidation).
  final Future<void> Function(String access, String refresh)? onTokensRefreshed;

  Future<Map<String, dynamic>> get(
    String path, {
    Map<String, String>? queryParams,
    String? idempotencyKey,
  }) async {
    final uri = _buildUri(path, queryParams);
    final response = await _sendWithRetry(
      () async => _http.get(uri, headers: await _headers(idempotencyKey)),
    );
    return _decode(response);
  }

  Future<Map<String, dynamic>> post(
    String path,
    Map<String, dynamic> body, {
    String? idempotencyKey,
  }) async {
    final uri = _buildUri(path, null);
    final response = await _sendWithRetry(
      () async => _http.post(
        uri,
        headers: await _headers(idempotencyKey),
        body: jsonEncode(body),
      ),
    );
    return _decode(response);
  }

  Future<Map<String, dynamic>> put(
    String path,
    Map<String, dynamic> body, {
    String? idempotencyKey,
  }) async {
    final uri = _buildUri(path, null);
    final response = await _sendWithRetry(
      () async => _http.put(
        uri,
        headers: await _headers(idempotencyKey),
        body: jsonEncode(body),
      ),
    );
    return _decode(response);
  }

  // ── Private helpers ───────────────────────────────────────────────────────

  Uri _buildUri(String path, Map<String, String>? queryParams) {
    final base = Uri.parse(baseUrl);
    return base.replace(
      path: '${base.path}$path',
      queryParameters: queryParams,
    );
  }

  Future<Map<String, String>> _headers(String? idempotencyKey) async {
    final token = await tokenStorage.readAccessToken();
    return {
      HttpHeaders.contentTypeHeader: ContentType.json.value,
      HttpHeaders.acceptHeader: ContentType.json.value,
      if (token != null) HttpHeaders.authorizationHeader: 'Bearer $token',
      if (idempotencyKey != null) 'Idempotency-Key': idempotencyKey,
    };
  }

  /// Sends the request; on 401 attempts a single token refresh then retries.
  Future<http.Response> _sendWithRetry(
    Future<http.Response> Function() send,
  ) async {
    final response = await send();
    if (response.statusCode != 401) return response;

    // Attempt silent refresh.
    final refreshed = await _tryRefresh();
    if (!refreshed) return response; // caller will see 401 → ApiError.

    return send(); // single retry with new token.
  }

  Future<bool> _tryRefresh() async {
    final refreshToken = await tokenStorage.readRefreshToken();
    if (refreshToken == null) return false;

    try {
      final uri = _buildUri('/auth/token/refresh', null);
      final response = await _http.post(
        uri,
        headers: {
          HttpHeaders.contentTypeHeader: ContentType.json.value,
          HttpHeaders.acceptHeader: ContentType.json.value,
        },
        body: jsonEncode({'refresh_token': refreshToken}),
      );
      if (response.statusCode != 200) return false;

      final json = jsonDecode(response.body) as Map<String, dynamic>;
      final newAccess = json['access_token'] as String;
      final newRefresh = json['refresh_token'] as String;
      await tokenStorage.saveTokens(
        accessToken: newAccess,
        refreshToken: newRefresh,
      );
      await onTokensRefreshed?.call(newAccess, newRefresh);
      return true;
    } catch (_) {
      // Refresh failed — caller will surface a 401 ApiError.
      return false;
    }
  }

  Map<String, dynamic> _decode(http.Response response) {
    final json = jsonDecode(response.body);
    if (response.statusCode >= 200 && response.statusCode < 300) {
      return json as Map<String, dynamic>;
    }
    final errorJson = json is Map<String, dynamic> ? json : <String, dynamic>{};
    final base = ApiError.fromJson(errorJson);
    // Attach status code for callers that need it (e.g. 409 optimistic lock).
    throw ApiError(
      category: base.category,
      correlationId: base.correlationId,
      statusCode: response.statusCode,
    );
  }
}
