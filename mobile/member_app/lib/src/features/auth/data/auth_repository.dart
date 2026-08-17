import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';
import '../../../core/providers.dart';

/// Auth repository — orchestrates OTP flow and token lifecycle.
class AuthRepository {
  const AuthRepository({
    required AuthApi authApi,
    required TokenStorage tokenStorage,
  })  : _authApi = authApi,
        _tokenStorage = tokenStorage;

  final AuthApi _authApi;
  final TokenStorage _tokenStorage;

  Future<void> requestOtp(String phone) =>
      _authApi.requestOtp(OtpChallengeRequest(phone: phone));

  Future<void> verifyOtpAndSave(String phone, String otp) async {
    final pair = await _authApi.verifyOtp(
      OtpVerifyRequest(phone: phone, otp: otp),
    );
    await _tokenStorage.saveTokens(
      accessToken: pair.accessToken,
      refreshToken: pair.refreshToken,
    );
  }

  Future<bool> isAuthenticated() => _tokenStorage.hasTokens();

  Future<void> logout() async {
    try {
      await _authApi.logout();
    } catch (_) {
      // Best-effort server-side revocation; always clear local tokens.
    }
    await _tokenStorage.clearTokens();
  }
}

final authRepositoryProvider = Provider<AuthRepository>((ref) => AuthRepository(
      authApi: ref.watch(authApiProvider),
      tokenStorage: ref.watch(tokenStorageProvider),
    ));
