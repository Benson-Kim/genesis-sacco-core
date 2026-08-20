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
import 'package:meta/meta.dart';

/// What the session layer needs from custody.
///
/// An interface rather than a concrete class so the session state machine can
/// be tested without a platform channel — the alternative is mocking a method
/// channel, which tests the mock rather than the machine.
abstract interface class TokenStore {
  Future<String?> readRefreshToken();
  Future<void> writeRefreshToken(String token);
  Future<String?> readCacheKey();
  Future<void> writeCacheKey(String base64Key);
  Future<void> clear();
}

/// Persistent custody of the member refresh token.
class TokenStorage implements TokenStore {
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

  @override
  Future<String?> readRefreshToken() => _storage.read(key: _refreshTokenKey);

  /// Persist BEFORE the token is first used. Callers must await this and only
  /// then issue the request that consumes the token.
  @override
  Future<void> writeRefreshToken(String token) =>
      _storage.write(key: _refreshTokenKey, value: token);

  @override
  Future<String?> readCacheKey() => _storage.read(key: _cacheKeyKey);

  @override
  Future<void> writeCacheKey(String base64Key) =>
      _storage.write(key: _cacheKeyKey, value: base64Key);

  /// Logout, 401, and refresh-reuse all land here. Deletes every member key —
  /// never a selective clear, because a half-cleared session is the state that
  /// produces cross-member leakage.
  @override
  Future<void> clear() async {
    await _storage.delete(key: _refreshTokenKey);
    await _storage.delete(key: _cacheKeyKey);
  }
}

/// An in-memory [TokenStore] for tests.
///
/// Kept beside the real one on purpose: a test double that drifts from the
/// interface it doubles is worse than no double at all, and `implements`
/// makes that drift a compile error.
@visibleForTesting
class InMemoryTokenStore implements TokenStore {
  String? _refreshToken;
  String? _cacheKey;

  /// Counts clears so a test can prove that a 401 purged custody exactly once.
  int clears = 0;

  @override
  Future<String?> readRefreshToken() async => _refreshToken;

  @override
  Future<void> writeRefreshToken(String token) async => _refreshToken = token;

  @override
  Future<String?> readCacheKey() async => _cacheKey;

  @override
  Future<void> writeCacheKey(String base64Key) async => _cacheKey = base64Key;

  @override
  Future<void> clear() async {
    clears++;
    _refreshToken = null;
    _cacheKey = null;
  }
}
