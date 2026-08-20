/// The error envelope must degrade, never crash — and must never widen the
/// server's disclosure.
///
/// Scaffold defect D7: the P16 client called `jsonDecode` unconditionally, so
/// an empty body or an intermediary's HTML error page threw a FormatException
/// on top of the failure being reported.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';

void main() {
  group('ApiError.fromResponse', () {
    test('decodes the {category, correlation_id} envelope', () {
      final ApiError error = ApiError.fromResponse(
        409,
        '{"category":"conflict","correlation_id":"c-123"}',
      );

      expect(error.kind, ApiFailureKind.conflict);
      expect(error.statusCode, 409);
      expect(error.category, 'conflict');
      expect(error.correlationId, 'c-123');
    });

    test('degrades on an empty body rather than throwing', () {
      final ApiError error = ApiError.fromResponse(500, '');

      expect(error.kind, ApiFailureKind.server);
      expect(error.category, isNull);
      expect(error.correlationId, isNull);
    });

    test('degrades on a non-JSON body (an intermediary error page)', () {
      final ApiError error = ApiError.fromResponse(502, '<html>Bad Gateway</html>');

      expect(error.kind, ApiFailureKind.server);
      expect(error.category, isNull);
    });

    test('degrades on JSON of the wrong shape', () {
      final ApiError error = ApiError.fromResponse(400, '["not","an","object"]');

      expect(error.kind, ApiFailureKind.server);
      expect(error.category, isNull);
    });

    test('ignores non-string field values instead of coercing them', () {
      final ApiError error = ApiError.fromResponse(
        400,
        '{"category":42,"correlation_id":{"nested":true}}',
      );

      expect(error.category, isNull);
      expect(error.correlationId, isNull);
    });
  });

  group('status mapping', () {
    test('401 is a session end, not a retryable failure', () {
      expect(ApiError.kindForStatus(401), ApiFailureKind.unauthenticated);
    });

    test('403 and 409 and 429 map to their own kinds', () {
      expect(ApiError.kindForStatus(403), ApiFailureKind.forbidden);
      expect(ApiError.kindForStatus(409), ApiFailureKind.conflict);
      expect(ApiError.kindForStatus(429), ApiFailureKind.rateLimited);
    });
  });

  group('log safety (FM-A)', () {
    test('toString carries neither the category nor the correlation id', () {
      // toString() reaches crash reporters and log sinks. Add either field to
      // it and this test fails - that is the guard.
      final ApiError error = ApiError.fromResponse(
        403,
        '{"category":"guarantee_not_actionable","correlation_id":"c-secret"}',
      );

      final String rendered = error.toString();

      expect(rendered, isNot(contains('c-secret')));
      expect(rendered, isNot(contains('guarantee_not_actionable')));
      expect(rendered, contains('403'));
    });
  });
}
