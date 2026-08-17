import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Secure token storage backed by the platform keychain / keystore.
/// Uses [FlutterSecureStorage] which encrypts on Android (EncryptedSharedPrefs)
/// and uses the iOS Keychain — satisfying MASTER_PROMPT §2.4 secure storage.
///
/// No PII is stored here — only opaque JWT strings (§1.6).
class TokenStorage {
  TokenStorage({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
              iOptions: IOSOptions(
                accessibility: KeychainAccessibility.first_unlock_this_device,
              ),
            );

  static const _accessKey = 'gp_access_token';
  static const _refreshKey = 'gp_refresh_token';

  final FlutterSecureStorage _storage;

  Future<void> saveTokens({
    required String accessToken,
    required String refreshToken,
  }) async {
    await Future.wait([
      _storage.write(key: _accessKey, value: accessToken),
      _storage.write(key: _refreshKey, value: refreshToken),
    ]);
  }

  Future<String?> readAccessToken() => _storage.read(key: _accessKey);

  Future<String?> readRefreshToken() => _storage.read(key: _refreshKey);

  Future<void> clearTokens() async {
    await Future.wait([
      _storage.delete(key: _accessKey),
      _storage.delete(key: _refreshKey),
    ]);
  }

  Future<bool> hasTokens() async {
    final access = await readAccessToken();
    return access != null && access.isNotEmpty;
  }
}
