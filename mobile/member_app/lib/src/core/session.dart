/// The member session state machine (scaffold defect D5).
///
/// Three properties, each of which the P16 scaffold violated:
///
/// 1. **Proactive, single-flight refresh.** The access token is refreshed
///    BEFORE a request when it is close to expiry — not reactively after a
///    401. Concurrent callers await one refresh, not ten. Refresh tokens
///    rotate and reuse revokes the whole family, so ten parallel refreshes are
///    not ten chances to succeed: they are nine reuses that kill the session.
///    Ported from the web's `getValidAccessToken` (`web/src/modules/auth/
///    session.ts`), which already solved this race.
/// 2. **Persist before use.** The new refresh token is written to secure
///    storage and awaited BEFORE the next request may consume it, so a crash
///    mid-rotation cannot leave the app holding a token the server retired.
/// 3. **Any 401 ends the session.** No retry, no second chance: discard both
///    tokens, purge the cache, return to login (`docs/member-auth.md`).
///
/// The access token lives in memory only and is never written anywhere.
library;

import 'dart:async';
import 'dart:convert';

import 'package:gp_api_client/gp_api_client.dart';

/// Refresh the token pair. Injected so the machine is testable without a
/// server, and so the transport does not depend on the session that owns it.
typedef RefreshTokens = Future<TokenPair> Function(String refreshToken);

/// What a refresh returns. The access token is held in memory; only
/// [refreshToken] is persisted.
class TokenPair {
  const TokenPair({
    required this.accessToken,
    required this.refreshToken,
    required this.expiresIn,
  });
  final String accessToken;
  final String refreshToken;

  /// `TokenResponse.expires_in`, as the server sent it.
  ///
  /// The access token's own `exp` claim is the better source and is preferred
  /// — it is absolute, so it survives a clock the device disagrees about. This
  /// is the fallback for when the claim cannot be read: an opaque token, a
  /// payload that is not JSON, an `exp` that is not an integer. Without it,
  /// such a token is treated as already expired and every single request
  /// triggers a refresh, which on a rotating family is a lot of rotation to
  /// pay for one unreadable field.
  final Duration expiresIn;
}

enum SessionState {
  /// No credential yet, or the session ended. The router sends this to login.
  signedOut,

  /// A refresh token exists and has not been refuted.
  signedIn,
}

/// Refresh this far ahead of expiry, so an in-flight request cannot be
/// overtaken by its own token expiring. Matches the web's margin.
const Duration _expiryMargin = Duration(seconds: 30);

class MemberSession {
  MemberSession({
    required TokenStore storage,
    required RefreshTokens refresh,
    DateTime Function() now = DateTime.now,
  })  : _storage = storage,
        _refresh = refresh,
        _now = now;

  final TokenStore _storage;
  final RefreshTokens _refresh;
  final DateTime Function() _now;

  /// In memory only, never persisted (FM-A).
  String? _accessToken;
  DateTime? _accessTokenExpiry;

  Future<String?>? _refreshInFlight;

  final StreamController<SessionState> _states =
      StreamController<SessionState>.broadcast();
  Stream<SessionState> get states => _states.stream;

  SessionState _state = SessionState.signedOut;
  SessionState get state => _state;

  /// Adopt the pair returned by `POST /member/auth/otp/verify`. The refresh
  /// token is persisted before this future completes, so the caller cannot
  /// race ahead and use a token that was never written down.
  Future<void> adopt(TokenPair pair) async {
    await _storage.writeRefreshToken(pair.refreshToken);
    _accessToken = pair.accessToken;
    _accessTokenExpiry = _expiryFor(pair);
    _transition(SessionState.signedIn);
  }

  /// Restore on cold start: a stored refresh token means "possibly signed in".
  ///
  /// Note it does NOT mean "signed in" — the scaffold treated mere token
  /// presence as authentication. The credential may have been revoked
  /// server-side, and the first request will find out; until then the app
  /// shows its restoring state rather than member data.
  Future<bool> restore() async {
    final String? stored = await _storage.readRefreshToken();
    if (stored == null) {
      _transition(SessionState.signedOut);
      return false;
    }
    _transition(SessionState.signedIn);
    return true;
  }

  /// The access token to send, refreshing first if it is missing or near
  /// expiry. Returns null when there is no live session.
  Future<String?> validAccessToken() {
    final String? token = _accessToken;
    final DateTime? expiry = _accessTokenExpiry;
    if (token != null &&
        expiry != null &&
        _now().add(_expiryMargin).isBefore(expiry)) {
      return Future<String?>.value(token);
    }
    // Single flight: every concurrent caller awaits the SAME refresh. Remove
    // this and each caller rotates the family, revoking the session.
    return _refreshInFlight ??= _performRefresh().whenComplete(() {
      _refreshInFlight = null;
    });
  }

  Future<String?> _performRefresh() async {
    final String? stored = await _storage.readRefreshToken();
    if (stored == null) {
      await end();
      return null;
    }
    final TokenPair pair;
    try {
      pair = await _refresh(stored);
    } on ApiError {
      // A refused refresh is a dead family. There is no retry that helps.
      await end();
      return null;
    }
    // Persist before use — the rotation invariant.
    await _storage.writeRefreshToken(pair.refreshToken);
    _accessToken = pair.accessToken;
    _accessTokenExpiry = _expiryFor(pair);
    return pair.accessToken;
  }

  /// The in-memory token, without triggering a refresh. The transport reads
  /// this synchronously; callers refresh via [validAccessToken] beforehand.
  String? get currentAccessToken => _accessToken;

  /// Any 401, an explicit logout, or a refused refresh. Idempotent.
  ///
  /// Member logout is local: there is no `/member/auth/logout` endpoint, and
  /// the scaffold's call to a staff logout route was defect D1.
  Future<void> end() async {
    _accessToken = null;
    _accessTokenExpiry = null;
    await _storage.clear();
    _transition(SessionState.signedOut);
  }

  void _transition(SessionState next) {
    if (_state == next) {
      return;
    }
    _state = next;
    _states.add(next);
  }

  /// When [pair]'s access token stops being usable.
  ///
  /// The token's own `exp` first, because it is absolute and authoritative.
  /// `expires_in` second, measured from now, for tokens whose claim cannot be
  /// read.
  DateTime _expiryFor(TokenPair pair) =>
      _expiryOf(pair.accessToken) ?? _now().add(pair.expiresIn);

  /// Read `exp` from the JWT payload, or null when it cannot be read.
  static DateTime? _expiryOf(String jwt) {
    final List<String> parts = jwt.split('.');
    if (parts.length != 3) {
      return null;
    }
    try {
      final String normalized = base64Url.normalize(parts[1]);
      final Object? payload =
          jsonDecode(utf8.decode(base64Url.decode(normalized)));
      if (payload is Map<String, Object?>) {
        final Object? exp = payload['exp'];
        if (exp is int) {
          return DateTime.fromMillisecondsSinceEpoch(exp * 1000, isUtc: true)
              .toLocal();
        }
      }
    } on FormatException {
      return null;
    }
    return null;
  }

  Future<void> dispose() => _states.close();
}
