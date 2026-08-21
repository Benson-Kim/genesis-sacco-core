/// The two-factor auth screens, and onboarding.
///
/// Every endpoint behind these is a PROPOSAL — none exist on `develop`. The
/// screens are rendered so the flow can be agreed before the server half is
/// written, which is the cheapest moment to change it.
///
/// The clock is fixed for every render. The code step shows a live countdown,
/// and a golden of a live countdown differs on every run and could never
/// become the regression gate the rest of them are.
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
import 'package:member_app/src/features/auth/presentation/pin_reset_screen.dart';
import 'package:member_app/src/features/auth/presentation/pin_sign_in_screen.dart';
import 'package:member_app/src/features/onboarding/presentation/onboarding_screen.dart';

import '../support/fakes.dart';

/// Anchors every challenge expiry, so countdowns render identically each run.
final DateTime _fixedNow = DateTime.utc(2026, 8, 21, 9);

void main() {
  setUpAll(loadGoldenFonts);

  Future<void> pump(
    WidgetTester tester,
    Widget screen, {
    FakeCredentialPort? port,
    GoldenDevice device = GoldenDevice.android,
  }) =>
      pumpGolden(
        tester,
        ProviderScope(
          overrides: <Override>[
            flavorProvider.overrideWithValue(Flavor.previewPinAuth),
            tokenStoreProvider.overrideWithValue(InMemoryTokenStore()),
            clockProvider.overrideWithValue(() => _fixedNow),
            credentialPortProvider.overrideWithValue(
              port ?? FakeCredentialPort(now: _fixedNow),
            ),
          ],
          child: screen,
        ),
        theme: GpTheme.light,
        device: device,
      );

  group('onboarding', () {
    testWidgets('the first page', (WidgetTester tester) async {
      await pump(tester, OnboardingScreen(onDone: () {}));
      await expectGolden(tester, 'onboarding_first');
    });

    testWidgets('the last page', (WidgetTester tester) async {
      await pump(tester, OnboardingScreen(onDone: () {}));

      await tester.tap(find.text('Next'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Next'));
      await tester.pumpAndSettle();

      await expectGolden(tester, 'onboarding_last');
    });
  });

  group('sign in with a PIN', () {
    testWidgets('member number and PIN', (WidgetTester tester) async {
      await pump(tester, const PinSignInScreen());
      await expectGolden(tester, 'pin_sign_in');
    });

    testWidgets('a rejected member number and PIN',
        (WidgetTester tester) async {
      // The single refusal: the server does not say which of the two was
      // wrong, and this screen could not tell even if it wanted to.
      await pump(
        tester,
        const PinSignInScreen(),
        port: FakeCredentialPort(now: _fixedNow, rejectCredentials: true),
      );

      await _signIn(tester);

      await expectGolden(tester, 'pin_sign_in_rejected');
    });

    testWidgets('a locked account', (WidgetTester tester) async {
      // The one failure a member IS told about specifically, because
      // retrying is hopeless and calling the SACCO is not.
      await pump(
        tester,
        const PinSignInScreen(),
        port: FakeCredentialPort(now: _fixedNow, lockedOut: true),
      );

      await _signIn(tester);

      await expectGolden(tester, 'pin_sign_in_locked');
    });

    testWidgets('the code step, naming where it went',
        (WidgetTester tester) async {
      // The destination is shown because the PIN has already been verified.
      // The OTP-only screen cannot do this, and the difference is the whole
      // argument for the flow.
      await pump(tester, const PinSignInScreen());

      await _signIn(tester);

      await expectGolden(tester, 'pin_sign_in_code');
    });
  });

  group('forgot PIN', () {
    testWidgets('asking for the member number', (WidgetTester tester) async {
      await pump(tester, const PinResetScreen());
      await expectGolden(tester, 'pin_reset_number');
    });

    testWidgets('the code step, naming nothing', (WidgetTester tester) async {
      // No destination here: nothing has been proven, so saying where the
      // code went would confirm the member number is real.
      await pump(tester, const PinResetScreen());

      await tester.enterText(find.byType(TextField), 'GP-00123');
      await tester.tap(find.text('Continue'));
      await tester.pump();
      await tester.pump();

      await expectGolden(tester, 'pin_reset_code');
    });

    testWidgets('choosing a new PIN', (WidgetTester tester) async {
      await pump(tester, const PinResetScreen());

      await tester.enterText(find.byType(TextField), 'GP-00123');
      await tester.tap(find.text('Continue'));
      await tester.pump();
      await tester.pump();
      await tester.enterText(find.byType(TextField).first, '123456');
      await tester.pump();
      await tester.pump();

      await expectGolden(tester, 'pin_reset_new');
    });
  });
}

/// Fill the credentials form and submit it.
Future<void> _signIn(WidgetTester tester) async {
  await tester.enterText(find.byType(TextField).at(0), 'GP-00123');
  await tester.enterText(find.byType(TextField).at(1), '246810');
  await tester.tap(find.text('Sign in'));
  await tester.pump();
  await tester.pump();
}
