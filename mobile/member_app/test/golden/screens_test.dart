/// Every screen, in every state worth reviewing.
///
/// # Where the sample figures live, and why it matters
///
/// Two home renders exist: `home` is what a member sees TODAY, with no read
/// endpoint merged and the hero saying so; `home_populated` is the same
/// screen once !7 lands, so the layout can be judged with figures in it.
///
/// The sample data for the second exists ONLY in this file. Nothing in `lib/`
/// has ever seen it, `homeSummaryProvider` returns null in the app, and the
/// populated render is reached by overriding that provider here. That is the
/// line rule 13 draws: a screen may be PREVIEWED against sample data and must
/// never SHIP against it and be ticked. Putting the sample in `lib/` behind a
/// debug flag is how that line gets crossed by accident.
///
/// # States are reached by driving the UI
///
/// The error and code panes are not constructed directly with a hand-written
/// message. They are reached by typing into the field and tapping the button,
/// through the real controller — so a golden of a rejected identifier is
/// evidence about what a member sees, rather than evidence that a string
/// literal renders.
@Tags(<String>['golden'])
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:gp_golden/gp_golden.dart';
import 'package:gp_ui/gp_ui.dart';
import 'package:member_app/src/core/env.dart';
import 'package:member_app/src/core/providers.dart';
import 'package:member_app/src/features/auth/presentation/sign_in_screen.dart';
import 'package:member_app/src/features/guarantees/domain/guarantee_view.dart';
import 'package:member_app/src/features/guarantees/presentation/guarantee_request_sheet.dart';
import 'package:member_app/src/features/home/domain/home_summary.dart';
import 'package:member_app/src/features/shell/presentation/app_shell.dart';

import '../support/fakes.dart';

/// Sample figures. Test-only, deliberately — see the library doc.
const HomeSummary _sample = HomeSummary(
  totalSavings: '248500.00',
  shares: '12000.00',
  loanBalance: '80000.00',
);

const GuaranteeView _request = GuaranteeView(
  id: '7f3a1c22-0000-4000-8000-000000000001',
  version: 3,
  figure: '150000.00',
  standing: GuaranteeStanding.awaitingDecision,
  reference: '7F3A1C22',
  borrowerReference: 'A41B90CE',
);

/// A flavor with everything switched on, for the populated previews only.
///
/// `Capabilities.asMergedToday` is what ships and every flag in it is false.
/// This exists so a reviewer can see the screens as they will be, without
/// that optimism ever reaching a build.
final Flavor _allOn = Flavor(
  name: 'preview',
  baseUrl: Uri.parse('https://preview.invalid'),
  tenantId: '00000000-0000-4000-8000-000000000001',
  pinSet: Flavor.dev.pinSet,
  pinEnforcement: Flavor.dev.pinEnforcement,
  capabilities: const Capabilities(
    guaranteeInbox: true,
    balances: true,
    transactions: true,
    statement: true,
    loans: true,
    deposits: true,
    loanApplication: true,
    notifications: true,
  ),
  inactivityTimeout: const Duration(minutes: 5),
);

void main() {
  setUpAll(loadGoldenFonts);

  Future<void> pumpScreen(
    WidgetTester tester,
    Widget screen, {
    List<Override> overrides = const <Override>[],
    GoldenDevice device = GoldenDevice.android,
  }) =>
      pumpGolden(
        tester,
        ProviderScope(
          overrides: <Override>[
            flavorProvider.overrideWithValue(Flavor.dev),
            tokenStoreProvider.overrideWithValue(InMemoryTokenStore()),
            ...overrides,
          ],
          child: screen,
        ),
        theme: GpTheme.light,
        device: device,
      );

  group('sign in', () {
    testWidgets('asking for an identifier', (WidgetTester tester) async {
      await pumpScreen(
        tester,
        const SignInScreen(),
        overrides: <Override>[
          authPortProvider.overrideWithValue(FakeAuthPort()),
        ],
      );
      await expectGolden(tester, 'sign_in_identifier');
    });

    testWidgets('a mistyped number, refused locally',
        (WidgetTester tester) async {
      await pumpScreen(
        tester,
        const SignInScreen(),
        overrides: <Override>[
          authPortProvider.overrideWithValue(FakeAuthPort()),
        ],
      );

      await tester.enterText(find.byType(TextField), '07123');
      await tester.tap(find.text('Send code'));
      await tester.pump();

      await expectGolden(tester, 'sign_in_identifier_error');
    });

    testWidgets('asking for the code', (WidgetTester tester) async {
      await pumpScreen(
        tester,
        const SignInScreen(),
        overrides: <Override>[
          authPortProvider.overrideWithValue(FakeAuthPort()),
        ],
      );

      await tester.enterText(find.byType(TextField), '0712345678');
      await tester.tap(find.text('Send code'));
      await tester.pump();
      await tester.pump();

      await expectGolden(tester, 'sign_in_code');
    });

    testWidgets('a rejected code', (WidgetTester tester) async {
      await pumpScreen(
        tester,
        const SignInScreen(),
        overrides: <Override>[
          authPortProvider.overrideWithValue(FakeAuthPort(rejectCode: true)),
        ],
      );

      await tester.enterText(find.byType(TextField), '0712345678');
      await tester.tap(find.text('Send code'));
      await tester.pump();
      await tester.pump();

      // Pasting the whole code into the first box is how an SMS autofill
      // arrives, and the field spreads it across all six.
      await tester.enterText(find.byType(TextField).first, '000000');
      await tester.pump();
      await tester.pump();

      await expectGolden(tester, 'sign_in_code_rejected');
    });
  });

  group('the shell', () {
    testWidgets('home, as it stands today', (WidgetTester tester) async {
      await pumpScreen(tester, const AppShell());
      await expectGolden(tester, 'home');
    });

    testWidgets('home, once the read endpoints land',
        (WidgetTester tester) async {
      await pumpScreen(
        tester,
        const AppShell(),
        overrides: <Override>[
          flavorProvider.overrideWithValue(_allOn),
          homeSummaryProvider.overrideWithValue(_sample),
        ],
      );
      await expectGolden(tester, 'home_populated');
    });

    testWidgets('home with figures concealed', (WidgetTester tester) async {
      await pumpScreen(
        tester,
        const AppShell(),
        overrides: <Override>[
          flavorProvider.overrideWithValue(_allOn),
          homeSummaryProvider.overrideWithValue(_sample),
        ],
      );

      await tester.tap(find.byIcon(Icons.visibility_rounded));
      await tester.pump();

      await expectGolden(tester, 'home_concealed');
    });

    testWidgets('home at 130% system text', (WidgetTester tester) async {
      // The first accessibility setting a SACCO member base turns on, and the
      // one that breaks fixed height rows. Rendered so it is reviewed rather
      // than discovered.
      await pumpScreen(
        tester,
        const AppShell(),
        overrides: <Override>[
          flavorProvider.overrideWithValue(_allOn),
          homeSummaryProvider.overrideWithValue(_sample),
        ],
        device: GoldenDevice.androidLargeText,
      );
      await expectGolden(tester, 'home_large_text');
    });

    testWidgets('accounts, locked', (WidgetTester tester) async {
      await pumpScreen(tester, const AppShell(initialTab: 1));
      await expectGolden(tester, 'accounts_locked');
    });

    testWidgets('more', (WidgetTester tester) async {
      await pumpScreen(tester, const AppShell(initialTab: 4));
      await expectGolden(tester, 'more');
    });
  });

  group('guarantee request', () {
    Future<void> pumpSheet(
      WidgetTester tester, {
      bool stale = false,
    }) =>
        pumpScreen(
          tester,
          const _SheetGround(
            child: GuaranteeRequestSheet(request: _request),
          ),
          overrides: <Override>[
            guaranteePortProvider
                .overrideWithValue(FakeGuaranteePort(stale: stale)),
          ],
        );

    testWidgets('choosing', (WidgetTester tester) async {
      await pumpSheet(tester);
      await expectGolden(tester, 'guarantee_choices');
    });

    testWidgets('confirming a consent', (WidgetTester tester) async {
      await pumpSheet(tester);

      await tester.tap(find.text('Agree to guarantee'));
      await tester.pump();

      await expectGolden(tester, 'guarantee_confirm');
    });

    testWidgets('recorded', (WidgetTester tester) async {
      await pumpSheet(tester);

      await tester.tap(find.text('Agree to guarantee'));
      await tester.pump();
      await tester.tap(find.text('Yes, guarantee this'));
      await tester.pump();
      await tester.pump();

      await expectGolden(tester, 'guarantee_recorded');
    });

    testWidgets('a stale version', (WidgetTester tester) async {
      await pumpSheet(tester, stale: true);

      await tester.tap(find.text('Agree to guarantee'));
      await tester.pump();
      await tester.tap(find.text('Yes, guarantee this'));
      await tester.pump();
      await tester.pump();

      await expectGolden(tester, 'guarantee_stale');
    });
  });
}

/// Puts the sheet on a plausible ground, so the golden shows a sheet rather
/// than a floating card on white.
class _SheetGround extends StatelessWidget {
  const _SheetGround({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) => Scaffold(
        backgroundColor: GpPalette.ink.withValues(alpha: 0.45),
        body: Align(
          alignment: Alignment.bottomCenter,
          child: Material(
            color: GpPalette.card,
            borderRadius: const BorderRadius.vertical(
              top: Radius.circular(GpRadius.hero),
            ),
            child: child,
          ),
        ),
      );
}
