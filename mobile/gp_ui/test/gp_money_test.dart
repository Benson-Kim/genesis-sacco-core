/// Money rendering guards.
///
/// The load-bearing test here is the round trip: strip the separators back
/// out and the result must be byte identical to the input. Grouping is the
/// one transformation this app performs on a monetary string, and that test
/// is what makes it safe to perform at all — it fails the moment grouping
/// starts altering, rounding or reordering a digit rather than just spacing
/// what it was given.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gp_ui/gp_ui.dart';

void main() {
  group('groupDigits inserts separators and changes nothing else', () {
    // Hand computed oracles, per the house testing rule.
    const Map<String, String> oracles = <String, String>{
      '0.00': '0.00',
      '1.50': '1.50',
      '999.99': '999.99',
      '1000.00': '1,000.00',
      '12345.67': '12,345.67',
      '248500.00': '248,500.00',
      '1234567.89': '1,234,567.89',
      '-4500.00': '-4,500.00',
    };

    oracles.forEach((String input, String expected) {
      test('$input renders as $expected', () {
        expect(groupDigits(input), expected);
      });
    });

    test('removing the separators returns the input, byte for byte', () {
      // This is the guard. Any change that computes rather than spaces —
      // rounding, reordering, dropping a trailing zero, parsing to a double
      // and back — fails here even if every oracle above still passes.
      for (final String input in oracles.keys) {
        expect(groupDigits(input).replaceAll(',', ''), input);
      }
    });
  });

  group('anything unexpected is returned untouched', () {
    for (final String odd in <String>[
      '248,500.00', // already grouped
      'KES 248500.00', // carries its currency
      '248500', // no decimal places
      '248500.0', // one decimal place
      '248500.000', // three
      '', // empty
      'unavailable', // not a figure at all
      '1e5.00', // exponent notation
    ]) {
      test('"$odd" passes through', () {
        expect(groupDigits(odd), odd);
      });
    }
  });

  group('the hidden mask', () {
    testWidgets('does not vary with the value behind it', (
      WidgetTester tester,
    ) async {
      // A mask sized to its input is not a mask: six dots versus three tells
      // a shoulder surfer the order of magnitude, which is the one thing
      // hiding a figure is supposed to withhold.
      Future<String> rendered(String value) async {
        await tester.pumpWidget(
          MaterialApp(home: Scaffold(body: GpMoney(value, hidden: true))),
        );
        return tester
            .widgetList<Text>(find.byType(Text))
            .map((Text t) => t.data ?? '')
            .join('|');
      }

      final String small = await rendered('1.00');
      final String large = await rendered('9876543.21');

      expect(small, large);
      expect(small, contains(GpMoney.mask));
    });

    testWidgets('keeps the currency visible so the row keeps its meaning', (
      WidgetTester tester,
    ) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(body: GpMoney('248500.00', hidden: true)),
        ),
      );

      expect(find.text('KES'), findsOneWidget);
      expect(find.text('248,500.00'), findsNothing);
    });
  });
}
