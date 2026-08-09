import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gp_ui/gp_ui.dart';

void main() {
  group('GpPalette', () {
    test('navy matches prototype hex #0F2C6B', () {
      expect(GpPalette.navy.value, equals(const Color(0xFF0F2C6B).value));
    });

    test('gold matches prototype hex #2E90FA', () {
      expect(GpPalette.gold.value, equals(const Color(0xFF2E90FA).value));
    });

    test('emerald matches prototype hex #1B7A54', () {
      expect(GpPalette.emerald.value, equals(const Color(0xFF1B7A54).value));
    });

    test('brick matches prototype hex #B23A2E', () {
      expect(GpPalette.brick.value, equals(const Color(0xFFB23A2E).value));
    });

    test('all palette colours are fully opaque', () {
      final colours = [
        GpPalette.navy,
        GpPalette.navyMid,
        GpPalette.navySoft,
        GpPalette.steel,
        GpPalette.gold,
        GpPalette.goldSoft,
        GpPalette.ink,
        GpPalette.sub,
        GpPalette.line,
        GpPalette.bg,
        GpPalette.panel,
        GpPalette.card,
        GpPalette.emerald,
        GpPalette.emeraldSoft,
        GpPalette.brick,
        GpPalette.brickSoft,
        GpPalette.orange,
        GpPalette.orangeSoft,
        GpPalette.loss,
      ];
      for (final c in colours) {
        expect(c.alpha, equals(255), reason: 'Expected $c to be fully opaque');
      }
    });
  });

  group('GpTheme', () {
    test('light theme primary is navy', () {
      expect(GpTheme.light.colorScheme.primary, equals(GpPalette.navy));
    });

    test('light theme uses Material 3', () {
      expect(GpTheme.light.useMaterial3, isTrue);
    });
  });

  group('GpStatCard widget', () {
    testWidgets('renders label and value', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: GpStatCard(label: 'Total Deposits', value: 'KES 1,200,000'),
          ),
        ),
      );
      expect(find.text('Total Deposits'), findsOneWidget);
      expect(find.text('KES 1,200,000'), findsOneWidget);
    });

    testWidgets('renders trend indicator when provided', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: GpStatCard(
              label: 'Loans',
              value: 'KES 500,000',
              trend: '+12%',
              trendUp: true,
            ),
          ),
        ),
      );
      expect(find.text('+12%'), findsOneWidget);
      expect(find.byIcon(Icons.arrow_upward_rounded), findsOneWidget);
    });
  });

  group('GpErrorView widget', () {
    testWidgets('shows category and correlation id', (tester) async {
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: GpErrorView(
              category: 'network_error',
              correlationId: 'abc-123',
            ),
          ),
        ),
      );
      expect(find.text('network_error'), findsOneWidget);
      expect(find.text('Ref: abc-123'), findsOneWidget);
    });

    testWidgets('retry button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: GpErrorView(
              category: 'timeout',
              onRetry: () => called = true,
            ),
          ),
        ),
      );
      await tester.tap(find.text('Retry'));
      expect(called, isTrue);
    });
  });
}
