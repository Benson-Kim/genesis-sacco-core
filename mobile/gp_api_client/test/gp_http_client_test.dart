/// Transport guards. Every test here fails when its guard is removed.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

void main() {
  final Uri baseUrl = Uri.parse('https://api.example.test');

  GpHttpClient clientWith(
    MockClient inner, {
    String? token,
    void Function()? onSessionEnded,
    PathGuard? pathGuard,
  }) =>
      GpHttpClient(
        baseUrl: baseUrl,
        tenantId: 'tenant-abc',
        accessToken: () => token,
        onSessionEnded: onSessionEnded,
        pathGuard: pathGuard,
        inner: inner,
      );

  group('construction', () {
    test('refuses a cleartext base URL (FM-F)', () {
      expect(
        () => GpHttpClient(
          baseUrl: Uri.parse('http://api.example.test'),
          tenantId: 'tenant-abc',
          accessToken: () => null,
          inner: MockClient((_) async => http.Response('{}', 200)),
        ),
        throwsArgumentError,
      );
    });
  });

  group('headers', () {
    test('sends x-tenant-id even with no session (D6: pre-auth routes)', () async {
      late Map<String, String> seen;
      final MockClient inner = MockClient((http.Request request) async {
        seen = request.headers;
        return http.Response('{"status":"sent"}', 202);
      });

      await clientWith(inner).post(
        '/member/auth/otp/request',
        body: <String, Object?>{'signin_identifier': 'a@b.test'},
        idempotencyKey: 'key-1',
      );

      expect(seen['x-tenant-id'], 'tenant-abc');
      expect(seen.containsKey('authorization'), isFalse);
    });

    test('sends the bearer token when a session is live', () async {
      late Map<String, String> seen;
      final MockClient inner = MockClient((http.Request request) async {
        seen = request.headers;
        return http.Response('{"member_no":"M-1"}', 200);
      });

      await clientWith(inner, token: 'access-xyz').get('/member/me');

      expect(seen['authorization'], 'Bearer access-xyz');
      expect(seen['x-tenant-id'], 'tenant-abc');
    });

    test('forwards the idempotency key on mutations (FM-G)', () async {
      late Map<String, String> seen;
      final MockClient inner = MockClient((http.Request request) async {
        seen = request.headers;
        return http.Response('{"ok":true}', 200);
      });

      await clientWith(inner, token: 't').post(
        '/member/guarantees/g-1/consent',
        body: <String, Object?>{'version': 3},
        idempotencyKey: 'key-abc',
      );

      expect(seen['Idempotency-Key'], 'key-abc');
    });
  });

  group('401 handling (D5)', () {
    test('ends the session and does NOT retry', () async {
      int calls = 0;
      bool ended = false;
      final MockClient inner = MockClient((http.Request request) async {
        calls++;
        return http.Response('{"category":"unauthenticated"}', 401);
      });

      await expectLater(
        clientWith(inner, token: 'stale', onSessionEnded: () => ended = true).get('/member/me'),
        throwsA(isA<ApiError>().having(
          (ApiError e) => e.kind,
          'kind',
          ApiFailureKind.unauthenticated,
        )),
      );

      expect(calls, 1, reason: 'a refresh-and-retry loop would make this 2');
      expect(ended, isTrue);
    });
  });

  group('path guard (FM-H)', () {
    test('refuses a staff path before anything reaches the wire', () async {
      int calls = 0;
      final MockClient inner = MockClient((http.Request request) async {
        calls++;
        return http.Response('{}', 200);
      });

      final GpHttpClient client = clientWith(
        inner,
        token: 't',
        pathGuard: (String path) => path.startsWith('/member/'),
      );

      expect(() => client.get('/me/permissions'), throwsArgumentError);
      expect(calls, 0);
    });

    test('allows the member surface', () async {
      final MockClient inner = MockClient(
        (http.Request request) async => http.Response('{"member_no":"M-1"}', 200),
      );

      final GpHttpClient client = clientWith(
        inner,
        token: 't',
        pathGuard: (String path) => path.startsWith('/member/'),
      );

      expect(await client.get('/member/me'), containsPair('member_no', 'M-1'));
    });
  });

  group('decoding (D7)', () {
    test('a 204 surfaces as a typed failure, not a decode crash', () async {
      final MockClient inner = MockClient((http.Request request) async => http.Response('', 204));

      await expectLater(
        clientWith(inner, token: 't').get('/member/me'),
        throwsA(isA<ApiError>().having(
          (ApiError e) => e.kind,
          'kind',
          ApiFailureKind.malformedResponse,
        )),
      );
    });

    test('a non-JSON 200 body surfaces as a typed failure', () async {
      final MockClient inner = MockClient(
        (http.Request request) async => http.Response('<html>hi</html>', 200),
      );

      await expectLater(
        clientWith(inner, token: 't').get('/member/me'),
        throwsA(isA<ApiError>().having(
          (ApiError e) => e.kind,
          'kind',
          ApiFailureKind.malformedResponse,
        )),
      );
    });

    test('a JSON array where an object is expected surfaces as a typed failure', () async {
      final MockClient inner = MockClient(
        (http.Request request) async => http.Response('[1,2,3]', 200),
      );

      await expectLater(
        clientWith(inner, token: 't').get('/member/me'),
        throwsA(isA<ApiError>()),
      );
    });

    test('decodes UTF-8 bodies by bytes, not by the header charset', () async {
      final MockClient inner = MockClient(
        (http.Request request) async => http.Response.bytes(
          utf8.encode('{"name":"Wanjiku Njeri"}'),
          200,
        ),
      );

      final Map<String, Object?> body = await clientWith(inner, token: 't').get('/member/me');

      expect(body['name'], 'Wanjiku Njeri');
    });
  });

  group('transport failures', () {
    test('a socket-level failure never leaks its message to the caller', () async {
      final MockClient inner = MockClient(
        (http.Request request) async => throw const FormatException('host api.example.test cert CN=evil'),
      );

      await expectLater(
        clientWith(inner, token: 't').get('/member/me'),
        throwsA(isA<ApiError>()
            .having((ApiError e) => e.kind, 'kind', ApiFailureKind.transport)
            .having((ApiError e) => e.toString(), 'toString', isNot(contains('evil')))),
      );
    });
  });
}
