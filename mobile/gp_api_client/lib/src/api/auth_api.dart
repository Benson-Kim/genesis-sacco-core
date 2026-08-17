import '../infrastructure/gp_http_client.dart';
import '../models/auth_models.dart';

/// Authentication API — wraps /auth/* endpoints.
class AuthApi {
  AuthApi(this._client);

  final GpHttpClient _client;

  /// Request an OTP challenge for [phone].
  Future<void> requestOtp(OtpChallengeRequest request) async {
    await _client.post('/auth/otp/request', request.toJson());
  }

  /// Verify OTP and receive a [TokenPair].
  Future<TokenPair> verifyOtp(OtpVerifyRequest request) async {
    final json = await _client.post('/auth/otp/verify', request.toJson());
    return TokenPair.fromJson(json);
  }

  /// Fetch the current user's permissions.
  Future<MePermissions> getMyPermissions() async {
    final json = await _client.get('/me/permissions');
    return MePermissions.fromJson(json);
  }

  /// Revoke the current session (logout).
  Future<void> logout() async {
    await _client.post('/auth/logout', {});
  }
}
