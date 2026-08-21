/// Member number and PIN rules.
///
/// The rules themselves are proposals — the backend that will own them does
/// not exist yet — but two of these guards are about behaviour that must not
/// drift whatever the format turns out to be.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:member_app/src/features/auth/domain/member_credential.dart';

void main() {
  group('member number', () {
    test('is sent exactly as typed, minus surrounding whitespace', () {
      // The same rule as the msisdn identifier: the server owns
      // normalisation, and a client that upper-cases or strips separators is
      // a second normaliser nobody tests against the first.
      expect(MemberNumber.parse('  gp-00123  ').number!.value, 'gp-00123');
      expect(MemberNumber.parse('GP/00123').number!.value, 'GP/00123');
    });

    test('accepts the plausible spellings', () {
      for (final String value in <String>[
        '00123',
        'GP-00123',
        'GP/00123',
        'BR1-000456',
      ]) {
        expect(MemberNumber.parse(value).number, isNotNull, reason: value);
      }
    });

    test('rejects only what is obviously not a member number', () {
      // Deliberately permissive while the real format is unknown. A strict
      // guess locks out real members, and being wrong in that direction is
      // worse than one extra round trip.
      expect(MemberNumber.parse('').problem, MemberNumberProblem.empty);
      expect(MemberNumber.parse('ab').problem, MemberNumberProblem.tooShort);
      expect(
        MemberNumber.parse('X' * 21).problem,
        MemberNumberProblem.tooLong,
      );
      expect(
        MemberNumber.parse('GP 00123').problem,
        MemberNumberProblem.badCharacters,
      );
    });
  });

  group('PIN for signing in', () {
    test('accepts a weak PIN, because it may be the real one', () {
      // The guard. A member whose existing PIN is 1234 must still be able to
      // sign in with it; refusing it at the login screen would lock them out
      // of their own money to make a point about PIN quality.
      expect(MemberPin.parse('1234', length: 4).pin, isNotNull);
      expect(MemberPin.parse('0000', length: 4).pin, isNotNull);
    });

    test('requires the configured length', () {
      expect(MemberPin.parse('12345', length: 6).problem,
          PinProblem.incomplete);
      expect(MemberPin.parse('123456', length: 6).pin, isNotNull);
    });

    test('refuses anything that is not digits', () {
      expect(MemberPin.parse('12a4', length: 4).problem,
          PinProblem.notNumeric);
    });
  });

  group('PIN being chosen', () {
    test('refuses all-the-same and sequences', () {
      expect(MemberPin.choose('1111', '1111', length: 4).problem,
          PinProblem.repeated);
      expect(MemberPin.choose('1234', '1234', length: 4).problem,
          PinProblem.sequential);
      expect(MemberPin.choose('4321', '4321', length: 4).problem,
          PinProblem.sequential);
    });

    test('a sequence is a run of one, not any pattern', () {
      // 1357 is a pattern and not a sequence. Refusing it would be the
      // client inventing policy the server never asked for.
      expect(MemberPin.choose('1357', '1357', length: 4).pin, isNotNull);
      expect(MemberPin.isSequential('1357'), isFalse);
      expect(MemberPin.isSequential('2468'), isFalse);
    });

    test('refuses a mismatched confirmation', () {
      expect(MemberPin.choose('2468', '2469', length: 4).problem,
          PinProblem.mismatch);
    });

    test('accepts a reasonable choice', () {
      expect(MemberPin.choose('285193', '285193', length: 6).pin, isNotNull);
    });
  });
}
