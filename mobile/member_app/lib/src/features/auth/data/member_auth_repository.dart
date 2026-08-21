/// [AuthPort] over `POST /member/auth/*`.
///
/// # The Idempotency-Key contract, which is not decorative here
///
/// `genesis.api.idempotency.IdempotencyMiddleware` runs on every mutating
/// request that carries an `Idempotency-Key` and resolves a tenant — and the
/// pre-auth OTP routes DO resolve one, from the `x-tenant-id` header, with the
/// empty string as actor. So these three routes are fully inside the
/// idempotency store, and its rules decide what the member experiences:
///
/// * **Same key, same body -> the stored response is replayed**, and the
///   handler never runs. Ask for a code twice under one key and the second ask
///   replays `{"status":"sent"}` without minting anything. The member stares
///   at a phone that will not buzz.
/// * **Same key, different body -> 409.** Not a retry: a hard conflict.
/// * **Any response under 500 is stored**, 401 and 429 included. A stored 429
///   replays as a 429 for the rest of the retention window, so a key that
///   caught a rate limit is a poisoned key.
///
/// Hence one slot per route, each rotating on exactly the event that makes the
/// intent new:
///
/// * **request** rotates on an explicit Resend (`freshIntent`). Retrying a
///   dropped send reuses the key and correctly replays; asking for a NEW code
///   is a new intent and must not.
/// * **verify** rotates whenever the code changes, which
///   [IdempotencyKeySlot.keyFor] does on its own. Re-submitting the SAME wrong
///   code replays the stored 401 instead of burning a server-side attempt —
///   which is the behaviour you want from a double-tap. Typing a corrected
///   code changes the body, rotates the key, and reaches the handler.
/// * **refresh** keys on the refresh token itself, and this is the one that
///   earns its keep. Rotation revokes the whole family on reuse. If the app
///   sends a refresh, the server rotates, and the response is lost to a dead
///   socket, then a naive retry with a FRESH key sends the old token again —
///   which the server has already retired — and reuse detection kills the
///   session. Keyed on the token, the retry replays the stored pair instead.
///   A dropped connection stops being a logout.
library;

import 'dart:convert';

import 'package:gp_api_client/gp_api_client.dart';

import '../../../core/session.dart';
import '../domain/auth_port.dart';
import '../domain/sign_in_identifier.dart';

/// The member auth surface, and nothing adjacent to it. `GpHttpClient`'s path
/// guard rejects anything outside `/member/` regardless (FM-H).
const String _otpRequestPath = '/member/auth/otp/request';
const String _otpVerifyPath = '/member/auth/otp/verify';
const String _refreshPath = '/member/auth/refresh';

class MemberAuthRepository implements AuthPort {
  MemberAuthRepository(this._client);

  final GpHttpClient _client;

  final IdempotencyKeySlot _requestSlot = IdempotencyKeySlot();
  final IdempotencyKeySlot _verifySlot = IdempotencyKeySlot();
  final IdempotencyKeySlot _refreshSlot = IdempotencyKeySlot();

  @override
  Future<void> requestOtp(
    SignInIdentifier identifier, {
    bool freshIntent = false,
  }) async {
    // `identifier`, never `email`. Both fields exist on OtpIdentifierBody and
    // exactly one may be set; `email` is documented as "accepted for one more
    // release" and only `identifier` accepts a mobile number. A client that
    // sent `email` would work today and lose phone sign-in on the release that
    // drops the field.
    final OtpRequestBody body = OtpRequestBody(identifier: identifier.value);
    final Map<String, Object?> json = body.toJson();
    if (freshIntent) {
      _requestSlot.reset();
    }
    await _client.post(
      _otpRequestPath,
      body: json,
      idempotencyKey: _requestSlot.keyFor(_canonical(json)),
    );
  }

  @override
  Future<TokenPair> verifyOtp(SignInIdentifier identifier, String code) async {
    final OtpVerifyBody body =
        OtpVerifyBody(identifier: identifier.value, code: code);
    final Map<String, Object?> json = body.toJson();
    final Map<String, Object?> response = await _client.post(
      _otpVerifyPath,
      body: json,
      idempotencyKey: _verifySlot.keyFor(_canonical(json)),
    );
    return _tokenPair(response);
  }

  @override
  Future<TokenPair> refresh(String refreshToken) async {
    final RefreshBody body = RefreshBody(refreshToken: refreshToken);
    final Map<String, Object?> json = body.toJson();
    final Map<String, Object?> response = await _client.post(
      _refreshPath,
      body: json,
      idempotencyKey: _refreshSlot.keyFor(_canonical(json)),
    );
    return _tokenPair(response);
  }

  /// The key's notion of "same body" must be the SERVER's notion of it: the
  /// middleware hashes the raw request bytes. So the canonical form is the
  /// same `jsonEncode` of the same map that `GpHttpClient.post` will send —
  /// not a summary, not a re-serialization with different key order.
  static String _canonical(Map<String, Object?> json) => jsonEncode(json);

  /// A 2xx whose body is not a [TokenResponse] is a failed call, not a crash.
  ///
  /// The generated `fromJson` casts, so a missing or wrongly typed field
  /// throws `TypeError` — an uncaught one, at the exact moment the member is
  /// signing in. `GpHttpClient` guarantees a decoded JSON object and no more
  /// than that; the SHAPE is checked here, and a surprise lands in the same
  /// [ApiFailureKind.malformedResponse] channel every other decode surprise
  /// uses (scaffold defect D7, one layer up).
  static TokenPair _tokenPair(Map<String, Object?> response) {
    final TokenResponse token;
    try {
      token = TokenResponse.fromJson(response);
    } on TypeError {
      throw const ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: 200,
      );
    }
    return TokenPair(
      accessToken: token.accessToken,
      refreshToken: token.refreshToken,
      expiresIn: Duration(seconds: token.expiresIn),
    );
  }
}
