/// Sign-in identifier rules (#35).
///
/// The load-bearing test in this file is the one that asserts what does NOT
/// happen: the client never rewrites a local number into E.164. Delete the
/// "sends what was typed" group and a helpful future edit that normalizes
/// client-side would pass everything else here while quietly becoming a second
/// normalizer, drifting from `genesis/domain/members.py` the first time either
/// side changes.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:member_app/src/features/auth/domain/sign_in_identifier.dart';

void main() {
  ({SignInIdentifier? identifier, IdentifierProblem? problem}) parse(
          String raw) =>
      SignInIdentifier.parse(raw);

  group('Kenya mobile numbers — the four accepted spellings', () {
    // Oracles taken from _KENYA_MSISDN_LOCAL / _KENYA_MSISDN_E164 in
    // backend/src/genesis/domain/members.py, which is the rule this mirrors.
    for (final String number in <String>[
      '0712345678',
      '0110123456',
      '+254712345678',
      '+254110123456',
    ]) {
      test('$number is a phone', () {
        final SignInIdentifier? identifier = parse(number).identifier;
        expect(identifier, isNotNull);
        expect(identifier!.kind, IdentifierKind.phone);
      });
    }
  });

  // Delete this group and a client-side rewrite would go unnoticed.
  group('sends what was typed: there is one normalizer, and it is the server',
      () {
    test('a local number is NOT rewritten into E.164', () {
      final SignInIdentifier? identifier = parse('0712345678').identifier;

      expect(identifier!.value, '0712345678');
      expect(identifier.value, isNot('+254712345678'));
    });

    test('an E.164 number is passed through unchanged', () {
      expect(parse('+254712345678').identifier!.value, '+254712345678');
    });

    test('surrounding whitespace is trimmed, and nothing else is touched', () {
      // The one transformation the server also performs
      // (normalize_kenya_msisdn does `raw.strip()`), so it cannot disagree.
      expect(parse('  0712345678  ').identifier!.value, '0712345678');
      expect(parse('  Member@Example.TEST ').identifier!.value,
          'Member@Example.TEST',
          reason: 'case folding an email address is a rewrite too');
    });
  });

  group('phone-ish input is held to the phone rule', () {
    // Why this matters: resolve_signin_identifier sends anything the msisdn
    // rule rejects down the EMAIL path "byte-identically". A mistyped number
    // therefore resolves no credential and still answers {"status":"sent"} —
    // correct for gate 1.6, and invisible to the member, who waits for a code
    // that was never going to be sent. Here is the only place it can be
    // caught.
    for (final String typo in <String>[
      '07123', // too short
      '0712345678901', // too long
      '0812345678', // 08 is not an accepted prefix
      '+255712345678', // wrong country
      '+254812345678', // accepted country, unaccepted prefix
      '0712 345 678', // spaced: the server's regexes do not allow it
    ]) {
      test('$typo is rejected as a malformed phone, not treated as an email',
          () {
        final ({SignInIdentifier? identifier, IdentifierProblem? problem})
            result = parse(typo);
        expect(result.identifier, isNull);
        expect(result.problem, IdentifierProblem.malformedPhone);
      });
    }
  });

  group('email addresses', () {
    test('an ordinary address is accepted', () {
      final SignInIdentifier? identifier = parse('member@example.test')
          .identifier;
      expect(identifier!.kind, IdentifierKind.email);
    });

    test('a plus-addressed local part is accepted', () {
      // Starts with a letter, so it is not phone-ish, and the '+' is fine.
      expect(parse('member+sacco@example.test').identifier, isNotNull);
    });

    for (final String bad in <String>[
      'member', // no @
      'member@', // no domain
      '@example.test', // no local part
      'member@example', // no dot in the domain
      'member@example.', // trailing dot
      'a@@b.test', // two @
    ]) {
      test('$bad is rejected as a malformed address', () {
        expect(parse(bad).problem, IdentifierProblem.malformedEmail);
      });
    }
  });

  group('length, mirroring Field(min_length=3, max_length=254)', () {
    test('empty is its own problem, so the message can be a prompt', () {
      expect(parse('').problem, IdentifierProblem.empty);
      expect(parse('   ').problem, IdentifierProblem.empty);
    });

    test('under three characters is refused before a round trip', () {
      expect(parse('ab').problem, IdentifierProblem.length);
    });

    test('over 254 characters is refused before a round trip', () {
      final String long = '${'a' * 250}@example.test';
      expect(long.length, greaterThan(254));
      expect(parse(long).problem, IdentifierProblem.length);
    });
  });
}
