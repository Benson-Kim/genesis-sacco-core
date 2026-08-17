import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';

void main() {
  group('ApiError', () {
    test('fromJson parses category and correlation_id', () {
      final error = ApiError.fromJson({
        'category': 'not_found',
        'correlation_id': 'abc-123',
      });
      expect(error.category, equals('not_found'));
      expect(error.correlationId, equals('abc-123'));
    });

    test('fromJson uses defaults for missing fields', () {
      final error = ApiError.fromJson({});
      expect(error.category, equals('unknown_error'));
      expect(error.correlationId, equals(''));
    });

    test('toString does not expose stack trace', () {
      final error = ApiError.fromJson({
        'category': 'server_error',
        'correlation_id': 'xyz-789',
      });
      final str = error.toString();
      expect(str, contains('server_error'));
      expect(str, contains('xyz-789'));
      // Must not contain anything that looks like a stack trace.
      expect(str, isNot(contains('#0')));
      expect(str, isNot(contains('dart:')));
    });
  });

  group('TokenPair', () {
    test('fromJson parses access and refresh tokens', () {
      final pair = TokenPair.fromJson({
        'access_token': 'access.jwt.here',
        'refresh_token': 'refresh.jwt.here',
      });
      expect(pair.accessToken, equals('access.jwt.here'));
      expect(pair.refreshToken, equals('refresh.jwt.here'));
    });
  });

  group('MemberSummary', () {
    test('fromJson parses all fields', () {
      final member = MemberSummary.fromJson({
        'id': 'uuid-1',
        'member_no': 'GP-0001',
        'full_name': 'Jane Doe',
        'type': 'person',
        'status': 'active',
        'share_balance': '50000.00',
        'deposit_balance': '120000.00',
      });
      expect(member.memberNo, equals('GP-0001'));
      expect(member.type, equals(MemberType.person));
      expect(member.status, equals(MemberStatus.active));
      // Balances are strings — never float (MASTER_PROMPT §0).
      expect(member.shareBalance, equals('50000.00'));
    });
  });

  group('PagedMembers', () {
    test('fromJson parses items and next_cursor', () {
      final paged = PagedMembers.fromJson({
        'items': [
          {
            'id': 'uuid-1',
            'member_no': 'GP-0001',
            'full_name': 'Jane Doe',
            'type': 'person',
            'status': 'active',
            'share_balance': '50000.00',
            'deposit_balance': '120000.00',
          }
        ],
        'next_cursor': 'cursor-abc',
      });
      expect(paged.items, hasLength(1));
      expect(paged.nextCursor, equals('cursor-abc'));
    });

    test('fromJson handles null next_cursor', () {
      final paged = PagedMembers.fromJson({
        'items': [],
        'next_cursor': null,
      });
      expect(paged.nextCursor, isNull);
    });
  });
}
