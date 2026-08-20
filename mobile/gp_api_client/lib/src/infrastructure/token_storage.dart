/// Token custody — `docs/member-auth.md` verbatim (scaffold defect D5).
///
/// Two rules the P16 scaffold broke:
///
/// 1. **The access token is never persisted.** It lives ≤15 minutes and is
///    re-minted from the refresh token on demand, so writing it to disk buys
///    nothing and widens the theft window (FM-A). Only the refresh token is
///    stored, in the platform keystore/keychain.
/// 2. **The newest refresh token is PERSISTED BEFORE FIRST USE.** Rotation
///    revokes the whole family on reuse, so a crash between "server issued a
///    new token" and "we wrote it down" must not leave the app holding a token
///    the server has already retired — that locks the member out until they
///    re-authenticate.
///
/// The member namespace is separate from any staff namespace by construction:
/// the member app links only this package and only ever writes `member.*`
/// keys, so a staff token cannot be read into a member session (FM-H).
library;

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Persistent custody of the member refresh token.
class TokenStorage {
  TokenStorage({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
              iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock_this_device),
            );

  final FlutterSecureStorage _storage;

  /// Namespaced so a staff build sharing the same device cannot collide with,
  /// or be mistaken for, a member session.
  static const String _refreshTokenKey = 'member.refresh_token';

  /// The cache-encryption key lives beside the refresh token: both are
  /// destroyed together on logout, so a purged session cannot leave a readable
  /// cache behind (FM-C).
  static const String _cacheKeyKey = 'member.cache_key';

  Future<String?> readRefreshToken() => _storage.read(key: _refreshTokenKey);

  /// Persist BEFORE the token is first used. Callers must await this and only
  /// then issue the request that consumes the token.
  Future<void> writeRefreshToken(String token) =>
      _storage.write(key: _refreshTokenKey, value: token);

  Future<String?> readCacheKey() => _storage.read(key: _cacheKeyKey);

  Future<void> writeCacheKey(String base64Key) =>
      _storage.write(key: _cacheKeyKey, value: base64Key);

  /// Logout, 401, and refresh-reuse all land here. Deletes every member key —
  /// never a selective clear, because a half-cleared session is the state that
  /// produces cross-member leakage.
  Future<void> clear() async {
    await _storage.delete(key: _refreshTokenKey);
    await _storage.delete(key: _cacheKeyKey);
  }
}
