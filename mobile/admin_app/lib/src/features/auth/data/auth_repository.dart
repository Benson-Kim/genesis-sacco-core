import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';
import '../../../core/providers.dart';

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

  Future<List<String>> verifyOtpAndSave(String phone, String otp) async {
    final pair = await _authApi.verifyOtp(
      OtpVerifyRequest(phone: phone, otp: otp),
    );
    await _tokenStorage.saveTokens(
      accessToken: pair.accessToken,
      refreshToken: pair.refreshToken,
    );
    // Fetch permissions immediately after login.
    final perms = await _authApi.getMyPermissions();
    return perms.permissions;
  }

  Future<bool> isAuthenticated() => _tokenStorage.hasTokens();

  Future<void> logout() async {
    try {
      await _authApi.logout();
    } catch (_) {
      // Best-effort; always clear local tokens.
    }
    await _tokenStorage.clearTokens();
  }
}

final authRepositoryProvider = Provider<AuthRepository>((ref) => AuthRepository(
      authApi: ref.watch(authApiProvider),
      tokenStorage: ref.watch(tokenStorageProvider),
    ));
