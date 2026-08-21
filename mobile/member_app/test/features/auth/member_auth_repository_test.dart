/// Wire-level guards for `POST /member/auth/*`.
///
/// These run against a real [GpHttpClient] with a [MockClient] underneath, not
/// against a stubbed client: the point is to see the bytes and headers that
/// actually go out. A double that agrees with the repository proves only that
/// they were written by the same person.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:member_app/src/core/session.dart';
import 'package:member_app/src/features/auth/data/member_auth_repository.dart';
import 'package:member_app/src/features/auth/domain/sign_in_identifier.dart';

void main() {
  final SignInIdentifier phone =
      SignInIdentifier.parse('0712345678').identifier!;

  /// Every request the repository made, in order.
  late List<http.Request> sent;

  MemberAuthRepository repositoryReturning(
    List<http.Response> Function(http.Request) responder,
  ) {
    sent = <http.Request>[];
    final MockClient inner = MockClient((http.Request request) async {
      sent.add(request);
      return responder(request).first;
    });
    return MemberAuthRepository(
      GpHttpClient(
        baseUrl: Uri.parse('https://api.example.test'),
        tenantId: '00000000-0000-4000-8000-000000000001',
        accessToken: () => null,
        inner: inner,
      ),
    );
  }

  MemberAuthRepository repositoryOk(String body, [int status = 200]) =>
      repositoryReturning((_) => <http.Response>[http.Response(body, status)]);

  const String tokenBody = '{"access_token":"a.b.c",'
      '"refresh_token":"r-1","expires_in":900}';

  Map<String, Object?> bodyOf(http.Request request) =>
      jsonDecode(request.body) as Map<String, Object?>;

  group('the identifier field (#35)', () {
    test('sends `identifier`, never `email`', () async {
      // `email` is documented as "accepted for one more release", and only
      // `identifier` accepts a mobile number. A client that sent `email` would
      // work today and lose phone sign-in the release the field is dropped.
      final MemberAuthRepository repository =
          repositoryOk('{"status":"sent"}', 202);

      await repository.requestOtp(phone);

      expect(bodyOf(sent.single)['identifier'], '0712345678');
      expect(bodyOf(sent.single)['email'], isNull);
    });

    test('names only the /member surface', () async {
      final MemberAuthRepository repository =
          repositoryOk('{"status":"sent"}', 202);

      await repository.requestOtp(phone);

      expect(sent.single.url.path, '/member/auth/otp/request');
    });

    test('sends the number exactly as typed', () async {
      final MemberAuthRepository repository =
          repositoryOk('{"status":"sent"}', 202);

      await repository.requestOtp(phone);

      expect(bodyOf(sent.single)['identifier'], isNot(startsWith('+254')));
    });
  });

  group('Idempotency-Key: the OTP request slot', () {
    test('a retry of the same send REUSES the key, so the server replays',
        () async {
      final MemberAuthRepository repository =
          repositoryOk('{"status":"sent"}', 202);

      await repository.requestOtp(phone);
      await repository.requestOtp(phone);

      expect(sent[0].headers['Idempotency-Key'],
          sent[1].headers['Idempotency-Key']);
    });

    test('an explicit resend ROTATES the key, or no second code is minted',
        () async {
      // The failure this prevents: same key + same body makes the middleware
      // replay the stored {"status":"sent"} without running the handler. The
      // member taps "send a new code", the app reports success, and no code is
      // ever sent. Remove `freshIntent` and this test fails.
      final MemberAuthRepository repository =
          repositoryOk('{"status":"sent"}', 202);

      await repository.requestOtp(phone);
      await repository.requestOtp(phone, freshIntent: true);

      expect(sent[0].headers['Idempotency-Key'],
          isNot(sent[1].headers['Idempotency-Key']));
    });
  });

  group('Idempotency-Key: the verify slot', () {
    test('resubmitting the SAME code reuses the key', () async {
      // A double-tap replays the stored response instead of burning a
      // server-side attempt against the member's lockout counter.
      final MemberAuthRepository repository = repositoryOk(tokenBody);

      await repository.verifyOtp(phone, '123456');
      await repository.verifyOtp(phone, '123456');

      expect(sent[0].headers['Idempotency-Key'],
          sent[1].headers['Idempotency-Key']);
    });

    test('a corrected code rotates the key, or the retry is a 409', () async {
      // Same key + DIFFERENT body is a hard conflict in the middleware, not a
      // retry. Without rotation, fixing a typo would answer 409 forever.
      final MemberAuthRepository repository = repositoryOk(tokenBody);

      await repository.verifyOtp(phone, '111111');
      await repository.verifyOtp(phone, '222222');

      expect(sent[0].headers['Idempotency-Key'],
          isNot(sent[1].headers['Idempotency-Key']));
    });
  });

  group('Idempotency-Key: the refresh slot (the one that saves a session)', () {
    test('retrying a refresh of the SAME token reuses the key', () async {
      // The scenario: the app sends a refresh, the server rotates the family
      // and answers, and the response is lost to a dead socket. A retry with a
      // fresh key would present the OLD token — which the server has already
      // retired — and reuse detection would revoke the whole family, logging
      // the member out for the sin of a bad connection. Keyed on the token,
      // the retry replays the stored pair instead.
      final MemberAuthRepository repository = repositoryOk(tokenBody);

      await repository.refresh('r-old');
      await repository.refresh('r-old');

      expect(sent[0].headers['Idempotency-Key'],
          sent[1].headers['Idempotency-Key']);
    });

    test('a rotated token is a new key', () async {
      final MemberAuthRepository repository = repositoryOk(tokenBody);

      await repository.refresh('r-old');
      await repository.refresh('r-new');

      expect(sent[0].headers['Idempotency-Key'],
          isNot(sent[1].headers['Idempotency-Key']));
    });
  });

  group('the key agrees with the server about what "the same body" is', () {
    test('the canonical body is byte-identical to what was sent', () async {
      // The middleware hashes the RAW request bytes. If the repository keyed
      // on a differently serialized form — different key order, a summary, a
      // re-encode — the client and the server would disagree about which
      // requests are the same request, and the guarantee would evaporate.
      final MemberAuthRepository repository = repositoryOk(tokenBody);

      await repository.verifyOtp(phone, '123456');

      expect(
        sent.single.body,
        jsonEncode(<String, Object?>{
          'code': '123456',
          'email': null,
          'identifier': '0712345678',
        }),
      );
    });
  });

  group('the token pair', () {
    test('carries expires_in, the fallback for an unreadable exp claim',
        () async {
      final MemberAuthRepository repository = repositoryOk(tokenBody);

      final TokenPair pair = await repository.verifyOtp(phone, '123456');

      expect(pair.accessToken, 'a.b.c');
      expect(pair.refreshToken, 'r-1');
      expect(pair.expiresIn, const Duration(seconds: 900));
    });

    test('a 2xx of the wrong shape is a typed failure, not a TypeError',
        () async {
      // The generated fromJson casts. Without the guard this throws an
      // uncaught TypeError at the exact moment the member is signing in.
      final MemberAuthRepository repository =
          repositoryOk('{"access_token":"a.b.c"}');

      await expectLater(
        repository.verifyOtp(phone, '123456'),
        throwsA(isA<ApiError>().having(
            (ApiError e) => e.kind, 'kind', ApiFailureKind.malformedResponse)),
      );
    });
  });

  group('failures arrive as ApiError', () {
    test('a rejected code surfaces as unauthenticated', () async {
      final MemberAuthRepository repository = repositoryReturning(
        (_) => <http.Response>[
          http.Response('{"category":"unauthenticated"}', 401)
        ],
      );

      await expectLater(
        repository.verifyOtp(phone, '000000'),
        throwsA(isA<ApiError>().having(
            (ApiError e) => e.kind, 'kind', ApiFailureKind.unauthenticated)),
      );
    });

    test('the auth rate guard surfaces as rateLimited', () async {
      final MemberAuthRepository repository = repositoryReturning(
        (_) =>
            <http.Response>[http.Response('{"category":"rate_limited"}', 429)],
      );

      await expectLater(
        repository.requestOtp(phone),
        throwsA(isA<ApiError>().having(
            (ApiError e) => e.kind, 'kind', ApiFailureKind.rateLimited)),
      );
    });
  });
}
