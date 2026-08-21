/// The sign-in flow, and the disclosure rules it has to keep.
///
/// The controller is exercised through a real [MemberSession] over an
/// [InMemoryTokenStore], with only the network faked. Token custody is the
/// half of this flow most worth checking, so it is the half least worth
/// stubbing.
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:member_app/src/core/providers.dart';
import 'package:member_app/src/core/session.dart';
import 'package:member_app/src/features/auth/domain/auth_port.dart';
import 'package:member_app/src/features/auth/domain/sign_in_controller.dart';
import 'package:member_app/src/features/auth/domain/sign_in_identifier.dart';

class _FakeAuth implements AuthPort {
  int requestCalls = 0;
  int verifyCalls = 0;
  final List<bool> freshIntents = <bool>[];

  ApiError? requestError;
  ApiError? verifyError;

  /// Set to hold a call open, so the busy guard can be observed mid-flight.
  Completer<void>? gate;

  TokenPair pair = const TokenPair(
    accessToken: 'header.payload.signature',
    refreshToken: 'r-1',
    expiresIn: Duration(seconds: 900),
  );

  @override
  Future<void> requestOtp(SignInIdentifier identifier,
      {bool freshIntent = false}) async {
    requestCalls++;
    freshIntents.add(freshIntent);
    await gate?.future;
    final ApiError? error = requestError;
    if (error != null) {
      throw error;
    }
  }

  @override
  Future<TokenPair> verifyOtp(SignInIdentifier identifier, String code) async {
    verifyCalls++;
    await gate?.future;
    final ApiError? error = verifyError;
    if (error != null) {
      throw error;
    }
    return pair;
  }

  @override
  Future<TokenPair> refresh(String refreshToken) async => pair;
}

void main() {
  late _FakeAuth auth;
  late InMemoryTokenStore store;
  late DateTime now;
  late ProviderContainer container;

  setUp(() {
    auth = _FakeAuth();
    store = InMemoryTokenStore();
    now = DateTime.utc(2026, 8, 21, 9);
    container = ProviderContainer(
      overrides: <Override>[
        authPortProvider.overrideWithValue(auth),
        tokenStoreProvider.overrideWithValue(store),
        clockProvider.overrideWithValue(() => now),
      ],
    );
    addTearDown(container.dispose);
  });

  SignInController controller() =>
      container.read(signInControllerProvider.notifier);
  SignInState state() => container.read(signInControllerProvider);
  MemberSession session() => container.read(sessionProvider);

  group('a malformed identifier never reaches the network', () {
    test('a mistyped number is refused locally', () async {
      await controller().submitIdentifier('07123');

      expect(auth.requestCalls, 0);
      expect(state().step, SignInStep.identifier);
      expect(state().message, contains('07XX'));
    });

    test('an empty field prompts rather than complains', () async {
      await controller().submitIdentifier('   ');

      expect(auth.requestCalls, 0);
      expect(state().message, contains('Enter your mobile number'));
    });
  });

  group('requesting a code', () {
    test('advances to the code step and records when it was sent', () async {
      await controller().submitIdentifier('0712345678');

      expect(auth.requestCalls, 1);
      expect(auth.freshIntents.single, isFalse);
      expect(state().step, SignInStep.code);
      expect(state().identifier!.value, '0712345678');
      expect(state().codeSentAt, now);
      expect(state().message, isNull);
      expect(state().busy, isFalse);
    });

    test('a transport failure stays put and says so', () async {
      auth.requestError =
          const ApiError(kind: ApiFailureKind.transport, statusCode: null);

      await controller().submitIdentifier('0712345678');

      expect(state().step, SignInStep.identifier);
      expect(state().busy, isFalse);
      expect(state().message, contains('could not reach'));
    });
  });

  group('least disclosure (gate 1.6)', () {
    test('the server category is never rendered', () async {
      // The category is the server's private vocabulary. A message built from
      // it would leak that vocabulary AND pin the UI to it. Feed the loudest
      // possible category and assert none of it survives into the message.
      auth.requestError = const ApiError(
        kind: ApiFailureKind.server,
        statusCode: 500,
        category: 'member_credential_not_found',
        correlationId: 'c-42',
      );

      await controller().submitIdentifier('0712345678');

      expect(state().message, isNot(contains('member_credential_not_found')));
      expect(state().message, isNot(contains('not_found')));
      // The correlation id IS shown: it identifies the request, not the
      // member, and it is the only way support can trace anything.
      expect(state().correlationId, 'c-42');
    });

    test('a rejected code says nothing about whether the account exists',
        () async {
      auth.verifyError = const ApiError(
        kind: ApiFailureKind.unauthenticated,
        statusCode: 401,
        category: 'unauthenticated',
      );
      await controller().submitIdentifier('0712345678');

      await controller().submitCode('000000');

      final String message = state().message!;
      expect(message, contains('code'));
      for (final String forbidden in <String>[
        'account',
        'member',
        'registered',
        'exists',
        'unknown',
      ]) {
        expect(message.toLowerCase(), isNot(contains(forbidden)),
            reason: 'a message that distinguishes a known identifier from an '
                'unknown one is an existence oracle');
      }
      // And the flow stays on the code step so the member can retype.
      expect(state().step, SignInStep.code);
      expect(session().state, SessionState.signedOut);
    });
  });

  group('verifying', () {
    test('a short code is refused locally', () async {
      await controller().submitIdentifier('0712345678');

      await controller().submitCode('12345');

      expect(auth.verifyCalls, 0);
      expect(state().message, contains('six digits'));
    });

    test('success adopts the session, persisting the token first', () async {
      await controller().submitIdentifier('0712345678');

      await controller().submitCode('123456');

      expect(auth.verifyCalls, 1);
      expect(state().step, SignInStep.done);
      expect(session().state, SessionState.signedIn);
      // Persist-before-use: by the time the flow reports done, custody holds
      // the refresh token.
      expect(await store.readRefreshToken(), 'r-1');
    });
  });

  group('resend', () {
    test('is refused inside the cooldown, without a request', () async {
      await controller().submitIdentifier('0712345678');

      await controller().resend();

      expect(auth.requestCalls, 1, reason: 'the resend must not have gone out');
      expect(state().message, contains('Hold on'));
    });

    test('rotates the key once the cooldown has passed', () async {
      await controller().submitIdentifier('0712345678');

      now = now.add(resendCooldown);
      await controller().resend();

      expect(auth.requestCalls, 2);
      expect(auth.freshIntents, <bool>[false, true],
          reason: 'a resend that reuses the key replays {"status":"sent"} '
              'and mints no second code');
    });
  });

  group('the busy guard (FM-G, first half)', () {
    test('a second submit while one is in flight is dropped', () async {
      auth.gate = Completer<void>();

      final Future<void> first = controller().submitIdentifier('0712345678');
      await controller().submitIdentifier('0712345678');

      expect(auth.requestCalls, 1);
      auth.gate!.complete();
      await first;
      expect(auth.requestCalls, 1);
    });
  });

  group('the flow resets when the session ends', () {
    test('an inactivity logout returns the flow to the identifier step',
        () async {
      await controller().submitIdentifier('0712345678');
      await controller().submitCode('123456');
      expect(state().step, SignInStep.done);

      await session().end();
      // The listener is on a broadcast stream, so let the event land.
      await Future<void>.delayed(Duration.zero);

      expect(state().step, SignInStep.identifier,
          reason: 'without the reset the member returns to a sign-in screen '
              'that renders its post-adoption spinner forever');
      expect(state().identifier, isNull);
    });
  });
}
