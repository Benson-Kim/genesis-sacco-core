/// The component sheet: every primitive, in every state, in one image.
///
/// This is a design review artifact first and a regression test second. It is
/// tagged `golden` so `mobile:goldens` can render and publish it while the
/// ordinary `mobile:test` run stays fast and headless.
///
/// The sheet renders on a tall synthetic canvas rather than a phone, because
/// its subject is the vocabulary rather than any one screen. Screen goldens
/// live in `member_app` and use real device sizes.
@Tags(<String>['golden'])
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gp_golden/gp_golden.dart';
import 'package:gp_ui/gp_ui.dart';

/// Tall enough to hold the whole sheet without scrolling it out of frame.
const GoldenDevice _sheet = GoldenDevice(
  name: 'sheet',
  size: Size(420, 1500),
);

void main() {
  testWidgets('foundations: colour and type', (WidgetTester tester) async {
    await pumpGolden(
      tester,
      const _Sheet(title: 'Foundations', child: _Foundations()),
      theme: GpTheme.light,
      device: _sheet,
    );
    await expectGolden(tester, 'foundations');
  });

  testWidgets('components: surfaces, controls, money',
      (WidgetTester tester) async {
    await pumpGolden(
      tester,
      const _Sheet(title: 'Components', child: _Components()),
      theme: GpTheme.light,
      device: _sheet,
    );
    await expectGolden(tester, 'components');
  });

  testWidgets('states: empty, not yet, and the locked tiles',
      (WidgetTester tester) async {
    await pumpGolden(
      tester,
      const _Sheet(title: 'States', child: _States()),
      theme: GpTheme.light,
      device: _sheet,
    );
    await expectGolden(tester, 'states');
  });
}

class _Sheet extends StatelessWidget {
  const _Sheet({required this.title, required this.child});

  final String title;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: GpPalette.bg,
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(GpSpace.xl),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              Text(title, style: GpTypography.displayLarge),
              const SizedBox(height: GpSpace.xl),
              child,
            ],
          ),
        ),
      ),
    );
  }
}

class _Label extends StatelessWidget {
  const _Label(this.text);

  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(
          top: GpSpace.xxl,
          bottom: GpSpace.md,
        ),
        child: GpSectionHeader(text, underline: true),
      );
}

class _Foundations extends StatelessWidget {
  const _Foundations();

  static const List<({String name, Color color})> _swatches =
      <({String name, Color color})>[
    (name: 'navy', color: GpPalette.navy),
    (name: 'navyMid', color: GpPalette.navyMid),
    (name: 'steel', color: GpPalette.steel),
    (name: 'navySoft', color: GpPalette.navySoft),
    (name: 'gold', color: GpPalette.gold),
    (name: 'goldSoft', color: GpPalette.goldSoft),
    (name: 'emerald', color: GpPalette.emerald),
    (name: 'brick', color: GpPalette.brick),
    (name: 'orange', color: GpPalette.orange),
    (name: 'ink', color: GpPalette.ink),
    (name: 'sub', color: GpPalette.sub),
    (name: 'line', color: GpPalette.line),
    (name: 'panel', color: GpPalette.panel),
    (name: 'bg', color: GpPalette.bg),
  ];

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const GpBrandLockup(eyebrow: 'Zuri Genesis · SACCO'),
        const _Label('Palette'),
        Wrap(
          spacing: GpSpace.sm,
          runSpacing: GpSpace.sm,
          children: <Widget>[
            for (final ({String name, Color color}) swatch in _swatches)
              SizedBox(
                width: 88,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Container(
                      height: 40,
                      decoration: BoxDecoration(
                        color: swatch.color,
                        borderRadius:
                            BorderRadius.circular(GpRadius.control),
                        border: Border.all(color: GpPalette.line),
                      ),
                    ),
                    const SizedBox(height: GpSpace.xs),
                    Text(swatch.name, style: GpTypography.bodySmall),
                  ],
                ),
              ),
          ],
        ),
        const _Label('Type scale'),
        const Text('Display 28/800', style: GpTypography.displayLarge),
        const SizedBox(height: GpSpace.sm),
        const Text('Headline 20/800', style: GpTypography.headlineMedium),
        const SizedBox(height: GpSpace.sm),
        const Text('Title 16/700', style: GpTypography.titleMedium),
        const SizedBox(height: GpSpace.sm),
        const Text('Title small 15/700', style: GpTypography.titleSmall),
        const SizedBox(height: GpSpace.sm),
        const Text('Body large 15/400 — the default reading size.',
            style: GpTypography.bodyLarge),
        const SizedBox(height: GpSpace.sm),
        const Text('Body medium 14/400 — secondary prose, muted.',
            style: GpTypography.bodyMedium),
        const SizedBox(height: GpSpace.sm),
        const Text('Body small 12/400 — captions and footnotes.',
            style: GpTypography.bodySmall),
        const SizedBox(height: GpSpace.sm),
        const Text('LABEL 12/600', style: GpTypography.labelSmall),
      ],
    );
  }
}

class _Components extends StatelessWidget {
  const _Components();

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const _Label('Balance hero'),
        const GpBalanceHero(
          title: 'Total savings',
          value: '248500.00',
          figures: <GpHeroFigure>[
            GpHeroFigure(label: 'Shares', value: '12000.00'),
            GpHeroFigure(label: 'Loan balance', value: '80000.00'),
          ],
          onToggleHidden: _noop,
        ),
        const SizedBox(height: GpSpace.md),
        const GpBalanceHero(
          title: 'Total savings',
          value: '248500.00',
          hidden: true,
          figures: <GpHeroFigure>[
            GpHeroFigure(label: 'Shares', value: '12000.00'),
            GpHeroFigure(label: 'Loan balance', value: '80000.00'),
          ],
          onToggleHidden: _noop,
        ),
        const _Label('Card and banners'),
        const GpCard(
          child: Text(
            'A card is white, one hairline border, 16px corners. The '
            'prototype is a border design and this is the whole of it.',
            style: GpTypography.bodyMedium,
          ),
        ),
        const SizedBox(height: GpSpace.md),
        const GpBanner(
          'Showing figures saved on this device.',
          icon: Icons.cloud_off_rounded,
        ),
        const SizedBox(height: GpSpace.sm),
        const GpBanner(
          'Your guarantee request was recorded.',
          tone: GpBannerTone.positive,
          icon: Icons.check_circle_rounded,
        ),
        const SizedBox(height: GpSpace.sm),
        const GpBanner(
          'Too many attempts. Wait a minute, then try again.',
          tone: GpBannerTone.danger,
          icon: Icons.error_rounded,
        ),
        const _Label('Pills'),
        const Wrap(
          spacing: GpSpace.sm,
          runSpacing: GpSpace.sm,
          children: <Widget>[
            GpPill('Pending'),
            GpPill('Active', tone: GpPillTone.positive),
            GpPill('In arrears', tone: GpPillTone.warning),
            GpPill('Defaulted', tone: GpPillTone.danger),
            GpPill('Guarantor', tone: GpPillTone.brand),
          ],
        ),
        const _Label('Buttons'),
        const GpPrimaryButton(label: 'Send code', onPressed: _noop),
        const SizedBox(height: GpSpace.sm),
        const GpPrimaryButton(
          label: 'Send code',
          onPressed: _noop,
          busy: true,
          busyLabel: 'Sending…',
        ),
        const SizedBox(height: GpSpace.sm),
        const GpPrimaryButton(label: 'Send code', onPressed: null),
        const SizedBox(height: GpSpace.sm),
        const GpSecondaryButton(
          label: 'Use a different number',
          onPressed: _noop,
        ),
        const SizedBox(height: GpSpace.sm),
        const Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            GpTextAction(label: 'Resend', onPressed: _noop, emphasis: true),
            GpTextAction(label: 'Change number', onPressed: _noop),
          ],
        ),
        const _Label('Money'),
        const GpCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              GpMoney('248500.00', size: GpMoneySize.large),
              SizedBox(height: GpSpace.sm),
              GpMoney('1234.50'),
              SizedBox(height: GpSpace.sm),
              GpMoney('248500.00', hidden: true),
              SizedBox(height: GpSpace.sm),
              Text(
                'Figures are server rendered strings, grouped by position '
                'and never parsed into numbers.',
                style: GpTypography.bodySmall,
              ),
            ],
          ),
        ),
        const _Label('Inputs'),
        const _FieldSample(),
      ],
    );
  }
}

class _FieldSample extends StatefulWidget {
  const _FieldSample();

  @override
  State<_FieldSample> createState() => _FieldSampleState();
}

class _FieldSampleState extends State<_FieldSample> {
  final TextEditingController _filled =
      TextEditingController(text: '0712345678');
  final TextEditingController _empty = TextEditingController();

  @override
  void dispose() {
    _filled.dispose();
    _empty.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        GpField(
          label: 'Mobile number or email',
          controller: _filled,
          hint: '07XX XXX XXX',
        ),
        const SizedBox(height: GpSpace.lg),
        GpField(
          label: 'Mobile number or email',
          controller: _empty,
          hint: '07XX XXX XXX',
          errorText: 'Enter a Kenyan mobile number as 07XX XXX XXX.',
        ),
        const SizedBox(height: GpSpace.lg),
        const GpOtpField(onChanged: _noopString, autofocus: false),
        const SizedBox(height: GpSpace.lg),
        const GpOtpField(
          onChanged: _noopString,
          autofocus: false,
          hasError: true,
        ),
      ],
    );
  }
}

class _States extends StatelessWidget {
  const _States();

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const _Label('Quick action tiles'),
        const GpCard(
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              GpActionTile(
                icon: Icons.south_rounded,
                label: 'Deposit',
                locked: true,
              ),
              GpActionTile(
                icon: Icons.north_rounded,
                label: 'Withdraw',
                locked: true,
              ),
              GpActionTile(
                icon: Icons.swap_horiz_rounded,
                label: 'Transfer',
                locked: true,
              ),
              GpActionTile(
                icon: Icons.payments_rounded,
                label: 'Repay loan',
                onTap: _noop,
              ),
            ],
          ),
        ),
        const _Label('Nothing to show, versus not built yet'),
        const GpCard(
          padding: EdgeInsets.zero,
          child: GpEmptyState(
            title: 'No transactions this month',
            message: 'Anything you deposit or withdraw will appear here.',
          ),
        ),
        const SizedBox(height: GpSpace.md),
        const GpCard(
          padding: EdgeInsets.zero,
          child: GpNotYetState(
            title: 'Statements are on the way',
            message:
                'Your SACCO is finishing this off. It will appear here as '
                'soon as it is ready, with nothing for you to do.',
          ),
        ),
        const _Label('Bottom navigation'),
        const GpCard(
          padding: EdgeInsets.zero,
          child: GpBottomNav(
            currentIndex: 0,
            onSelected: _noopInt,
            destinations: <GpNavDestination>[
              GpNavDestination(
                icon: Icons.home_outlined,
                selectedIcon: Icons.home_rounded,
                label: 'Home',
              ),
              GpNavDestination(
                icon: Icons.account_balance_wallet_outlined,
                selectedIcon: Icons.account_balance_wallet_rounded,
                label: 'Accounts',
                locked: true,
              ),
              GpNavDestination(
                icon: Icons.swap_vert_rounded,
                selectedIcon: Icons.swap_vert_rounded,
                label: 'Transact',
                locked: true,
              ),
              GpNavDestination(
                icon: Icons.request_quote_outlined,
                selectedIcon: Icons.request_quote_rounded,
                label: 'Loans',
                locked: true,
              ),
              GpNavDestination(
                icon: Icons.more_horiz_rounded,
                selectedIcon: Icons.more_horiz_rounded,
                label: 'More',
              ),
            ],
          ),
        ),
      ],
    );
  }
}

void _noop() {}
void _noopInt(int _) {}
void _noopString(String _) {}
