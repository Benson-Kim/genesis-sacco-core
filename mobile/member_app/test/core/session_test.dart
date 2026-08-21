/// Session state-machine guards. Each test fails when its guard is removed.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:member_app/src/core/session.dart';

/// Mint a JWT whose payload carries only `exp`. Signature is not checked
/// client-side — the server is the only party that may trust a token — so the
/// third segment is filler.
String jwtExpiringAt(DateTime expiry) {
  String segment(Map<String, Object?> payload) =>
      base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
  final String header =
      segment(<String, Object?>{'alg': 'EdDSA', 'typ': 'JWT'});
  final String body = segment(
      <String, Object?>{'exp': expiry.toUtc().millisecondsSinceEpoch ~/ 1000});
  return '$header.$body.signature-not-verified-client-side';
}

/// The server's `expires_in`, as it comes back from `/member/auth/*`.
///
/// Only consulted when the token's own `exp` cannot be read, so most tests
/// here take the default and never exercise it.
const Duration _serverLifetime = Duration(minutes: 15);

TokenPair pairOf(
  String accessToken,
  String refreshToken, {
  Duration expiresIn = _serverLifetime,
}) =>
    TokenPair(
      accessToken: accessToken,
      refreshToken: refreshToken,
      expiresIn: expiresIn,
    );

void main() {
  late InMemoryTokenStore store;
  late DateTime clock;

  setUp(() {
    store = InMemoryTokenStore();
    clock = DateTime.utc(2026, 8, 20, 12);
  });

  MemberSession sessionWith(RefreshTokens refresh) => MemberSession(
        storage: store,
        refresh: refresh,
        now: () => clock,
      );

  group('adopt', () {
    test('persists the refresh token before the future completes', () async {
      final MemberSession session =
          sessionWith((_) async => throw StateError('unused'));

      await session.adopt(pairOf(
        jwtExpiringAt(clock.add(const Duration(minutes: 15))),
        'refresh-1',
      ));

      expect(await store.readRefreshToken(), 'refresh-1');
      expect(session.state, SessionState.signedIn);
    });

    test('never persists the access token', () async {
      final String access =
          jwtExpiringAt(clock.add(const Duration(minutes: 15)));
      final MemberSession session =
          sessionWith((_) async => throw StateError('unused'));

      await session.adopt(pairOf(access, 'refresh-1'));

      // Custody holds the refresh token and the cache key. Nothing else.
      expect(await store.readRefreshToken(), isNot(access));
      expect(await store.readCacheKey(), isNull);
    });
  });

  group('validAccessToken', () {
    test('reuses a token that is comfortably live, without refreshing',
        () async {
      int refreshes = 0;
      final MemberSession session = sessionWith((String t) async {
        refreshes++;
        return pairOf(jwtExpiringAt(clock), 'r2');
      });
      final String access =
          jwtExpiringAt(clock.add(const Duration(minutes: 15)));
      await session.adopt(pairOf(access, 'r1'));

      expect(await session.validAccessToken(), access);
      expect(refreshes, 0);
    });

    test('refreshes PROACTIVELY inside the 30s expiry margin', () async {
      // Oracle: margin is 30s. A token expiring in 20s is inside it, so the
      // machine must refresh BEFORE the request rather than after a 401.
      int refreshes = 0;
      final String fresh =
          jwtExpiringAt(clock.add(const Duration(minutes: 15)));
      final MemberSession session = sessionWith((String t) async {
        refreshes++;
        return pairOf(fresh, 'r2');
      });
      await session.adopt(pairOf(
        jwtExpiringAt(clock.add(const Duration(seconds: 20))),
        'r1',
      ));

      expect(await session.validAccessToken(), fresh);
      expect(refreshes, 1);
    });

    test('FM5: ten concurrent callers trigger EXACTLY ONE refresh', () async {
      // The falsifiable core. Refresh tokens rotate and reuse revokes the
      // family, so ten parallel refreshes would revoke the session nine times
      // over. Delete the single-flight latch in validAccessToken and this
      // expectation becomes 10.
      int refreshes = 0;
      final Completer<void> gate = Completer<void>();
      final String fresh =
          jwtExpiringAt(clock.add(const Duration(minutes: 15)));
      final MemberSession session = sessionWith((String t) async {
        refreshes++;
        await gate.future;
        return pairOf(fresh, 'r2');
      });
      await session.adopt(pairOf(
        jwtExpiringAt(clock.subtract(const Duration(minutes: 1))),
        'r1',
      ));

      final List<Future<String?>> callers = List<Future<String?>>.generate(
        10,
        (_) => session.validAccessToken(),
      );
      gate.complete();
      final List<String?> results = await Future.wait(callers);

      expect(refreshes, 1);
      expect(results, everyElement(fresh));
    });

    test(
        'the refresh token is persisted before the new access token is handed out',
        () async {
      // Persist-before-use: if the write happened after the token was
      // returned, a crash in between would leave the app holding a token the
      // server has already retired.
      String? storedAtHandout;
      final MemberSession session = sessionWith((String t) async => pairOf(
            jwtExpiringAt(clock.add(const Duration(minutes: 15))),
            'r2',
          ));
      await session.adopt(pairOf(
        jwtExpiringAt(clock.subtract(const Duration(minutes: 1))),
        'r1',
      ));

      await session.validAccessToken();
      storedAtHandout = await store.readRefreshToken();

      expect(storedAtHandout, 'r2');
    });

    test('a refused refresh ends the session and purges custody', () async {
      final MemberSession session = sessionWith(
        (String t) async => throw const ApiError(
          kind: ApiFailureKind.unauthenticated,
          statusCode: 401,
        ),
      );
      await session.adopt(pairOf(
        jwtExpiringAt(clock.subtract(const Duration(minutes: 1))),
        'r1',
      ));

      expect(await session.validAccessToken(), isNull);
      expect(session.state, SessionState.signedOut);
      expect(await store.readRefreshToken(), isNull);
      expect(store.clears, 1);
    });

    test('a second refresh may run after the first completes', () async {
      // The latch is per-flight, not permanent: a session that refreshed once
      // must be able to refresh again when the new token also ages out.
      int refreshes = 0;
      final MemberSession session = sessionWith((String t) async {
        refreshes++;
        return pairOf(
          jwtExpiringAt(clock.subtract(const Duration(minutes: 1))),
          'r${refreshes + 1}',
        );
      });
      await session.adopt(pairOf(
        jwtExpiringAt(clock.subtract(const Duration(minutes: 1))),
        'r1',
      ));

      await session.validAccessToken();
      await session.validAccessToken();

      expect(refreshes, 2);
    });
  });

  group('restore', () {
    test('no stored token means signed out', () async {
      final MemberSession session =
          sessionWith((_) async => throw StateError('unused'));

      expect(await session.restore(), isFalse);
      expect(session.state, SessionState.signedOut);
    });

    test('a stored token restores the session without asserting authentication',
        () async {
      await store.writeRefreshToken('r1');
      final MemberSession session =
          sessionWith((_) async => throw StateError('unused'));

      expect(await session.restore(), isTrue);
      // Presence is not proof: no access token has been minted yet. The
      // scaffold treated hasTokens() as authenticated (defect D5).
      expect(session.currentAccessToken, isNull);
    });
  });

  group('end', () {
    test('is idempotent and always purges', () async {
      final MemberSession session =
          sessionWith((_) async => throw StateError('unused'));
      await session.adopt(pairOf(
        jwtExpiringAt(clock.add(const Duration(minutes: 15))),
        'r1',
      ));

      await session.end();
      await session.end();

      expect(session.state, SessionState.signedOut);
      expect(session.currentAccessToken, isNull);
      expect(store.clears, 2);
    });
  });

  group('an access token whose exp cannot be read', () {
    // MR-0 treated any unreadable token as already expired, on the reasoning
    // that "refreshing needlessly is cheap". MR-1 overturns that, because on a
    // ROTATING family it is not cheap and never was: every needless refresh
    // rotates the family, and each rotation is a chance for a dropped response
    // to strand the app holding a retired token, which reuse detection answers
    // by revoking everything. Refreshing on every single request was the
    // riskiest thing this client could have done with an unparseable token.
    //
    // `expires_in` is the server's own statement of the token's lifetime,
    // delivered over the same TLS-verified response as the token itself. A
    // client that will use the token but not believe the lifetime shipped
    // beside it is drawing a line with nothing on either side of it. And the
    // safety net is unchanged: if the token really is dead, the server answers
    // 401 and the session ends — once, instead of after every request.

    test('falls back to expires_in rather than refreshing on every call',
        () async {
      int refreshes = 0;
      final MemberSession session = sessionWith((String t) async {
        refreshes++;
        return pairOf(
            jwtExpiringAt(clock.add(const Duration(minutes: 15))), 'r2');
      });

      await session.adopt(
          pairOf('not-a-jwt', 'r1', expiresIn: const Duration(minutes: 15)));

      expect(await session.validAccessToken(), 'not-a-jwt');
      expect(refreshes, 0,
          reason: 'delete the expires_in fallback and this becomes 1 — and '
              'one rotation per request in production');
    });

    test('an expires_in that has already elapsed still refreshes', () async {
      // The fallback is a lifetime, not a blank cheque: a token the server
      // said would live 20 seconds is inside the 30s margin immediately.
      int refreshes = 0;
      final String fresh =
          jwtExpiringAt(clock.add(const Duration(minutes: 15)));
      final MemberSession session = sessionWith((String t) async {
        refreshes++;
        return pairOf(fresh, 'r2');
      });

      await session.adopt(
          pairOf('not-a-jwt', 'r1', expiresIn: const Duration(seconds: 20)));

      expect(await session.validAccessToken(), fresh);
      expect(refreshes, 1);
    });

    test('a readable exp still wins over expires_in', () async {
      // The claim is absolute and survives a device clock the server does not
      // share; expires_in is measured from whenever we happened to receive it.
      // A pair whose two sources disagree must follow the claim.
      int refreshes = 0;
      final String fresh =
          jwtExpiringAt(clock.add(const Duration(minutes: 15)));
      final MemberSession session = sessionWith((String t) async {
        refreshes++;
        return pairOf(fresh, 'r2');
      });

      await session.adopt(pairOf(
        jwtExpiringAt(clock.subtract(const Duration(minutes: 1))),
        'r1',
        expiresIn: const Duration(hours: 1),
      ));

      expect(refreshes, 0);
      expect(await session.validAccessToken(), fresh);
      expect(refreshes, 1,
          reason: 'the expired claim must beat the generous expires_in');
    });
  });
}
