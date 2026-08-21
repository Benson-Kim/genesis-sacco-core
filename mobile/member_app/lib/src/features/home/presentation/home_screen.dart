/// Home.
///
/// The layout is the one that ships when the read endpoints land: a navy
/// balance hero, a row of quick actions, guarantee requests, recent activity.
/// What differs today is only the DATA — there is none, because no merged
/// endpoint returns a member their own figures.
///
/// That is why the hero renders an honest "not available yet" rather than a
/// placeholder number or a shimmer. A shimmer says "loading", and nothing is
/// loading; a zero says the member has nothing, which is a lie about their
/// money. The hero keeps its exact shipping shape either way, so what is
/// reviewed now is what will be seen later, and !7 landing changes a value
/// rather than a screen.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../../../core/env.dart';
import '../../../core/providers.dart';
import '../domain/home_summary.dart';

class HomeScreen extends ConsumerWidget {
  const HomeScreen({super.key});

  /// The Favorites default set (#43, review §3.2). The order is fixed so a
  /// member's thumb learns it once; only the locks change between releases.
  static const List<({IconData icon, String label})> quickActions =
      <({IconData icon, String label})>[
    (icon: Icons.south_rounded, label: 'Deposit'),
    (icon: Icons.north_rounded, label: 'Withdraw'),
    (icon: Icons.swap_horiz_rounded, label: 'Transfer'),
    (icon: Icons.payments_rounded, label: 'Repay loan'),
  ];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final Capabilities capabilities = ref.watch(flavorProvider).capabilities;
    final bool hidden = ref.watch(balancesHiddenProvider);
    // Null today, and truthfully so. See homeSummaryProvider.
    final HomeSummary? summary = ref.watch(homeSummaryProvider);

    return ListView(
      padding: const EdgeInsets.fromLTRB(
        GpSpace.gutter,
        GpSpace.lg,
        GpSpace.gutter,
        GpSpace.xxxl,
      ),
      children: <Widget>[
        const _Greeting(),
        const SizedBox(height: GpSpace.lg),
        GpBalanceHero(
          title: 'Total savings',
          value: summary?.totalSavings,
          hidden: hidden,
          // No figure means nothing to conceal, so the eye control is absent
          // rather than present and inert.
          onToggleHidden: summary == null
              ? null
              : () => ref.read(balancesHiddenProvider.notifier).state = !hidden,
          unavailableMessage: 'Your balances will appear here once your '
              'SACCO switches them on.',
          figures: <GpHeroFigure>[
            GpHeroFigure(label: 'Shares', value: summary?.shares),
            GpHeroFigure(label: 'Loan balance', value: summary?.loanBalance),
          ],
        ),
        const SizedBox(height: GpSpace.xxl),
        const GpSectionHeader('Quick actions'),
        const SizedBox(height: GpSpace.md),
        GpCard(
          padding: const EdgeInsets.symmetric(
            horizontal: GpSpace.md,
            vertical: GpSpace.lg,
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceAround,
            children: <Widget>[
              for (final ({IconData icon, String label}) action
                  in quickActions)
                Expanded(
                  child: GpActionTile(
                    icon: action.icon,
                    label: action.label,
                    // The capability map is the only thing that decides.
                    // There is no runtime probe, so a server cannot enable a
                    // screen this build has no code for.
                    locked: !_unlocked(action.label, capabilities),
                  ),
                ),
            ],
          ),
        ),
        const SizedBox(height: GpSpace.xxl),
        const GpSectionHeader('Guarantee requests'),
        const SizedBox(height: GpSpace.md),
        GpCard(
          padding: EdgeInsets.zero,
          // The two ACTS behind this are merged and fully built; what is
          // missing is the list that would let a member reach one (#41).
          // So the section is a not-yet rather than an empty: telling a
          // member they have no requests would be a claim this app cannot
          // check.
          child: capabilities.guaranteeInbox
              ? const GpEmptyState(
                  title: 'No requests right now',
                  message: 'When another member asks you to guarantee a '
                      'loan, it will appear here.',
                  icon: Icons.handshake_outlined,
                )
              : const GpNotYetState(
                  title: 'Guarantee requests are coming',
                  message: 'You will be able to review and respond to '
                      'requests here.',
                  icon: Icons.handshake_outlined,
                ),
        ),
        const SizedBox(height: GpSpace.xxl),
        const GpSectionHeader('Recent activity'),
        const SizedBox(height: GpSpace.md),
        GpCard(
          padding: EdgeInsets.zero,
          child: capabilities.transactions
              ? const GpEmptyState(
                  title: 'Nothing yet this month',
                  message: 'Anything you pay in or take out will show here.',
                  icon: Icons.receipt_long_outlined,
                )
              : const GpNotYetState(
                  title: 'Your activity is coming',
                  message: 'Deposits, withdrawals and repayments will be '
                      'listed here.',
                  icon: Icons.receipt_long_outlined,
                ),
        ),
      ],
    );
  }

  static bool _unlocked(String label, Capabilities capabilities) {
    switch (label) {
      case 'Deposit':
      case 'Withdraw':
      case 'Transfer':
        return capabilities.deposits;
      case 'Repay loan':
        return capabilities.loans;
      default:
        return false;
    }
  }
}

/// The one personal line on the screen.
///
/// It does not greet by name, because no merged endpoint tells this app the
/// member's name. "Welcome back" is true, unremarkable, and does not invent
/// an intimacy the app has not earned. A hardcoded placeholder name would be
/// worse than none, and greeting a UUID would be absurd.
class _Greeting extends StatelessWidget {
  const _Greeting();

  @override
  Widget build(BuildContext context) => const Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text('Welcome back', style: GpTypography.displayLarge),
          SizedBox(height: GpSpace.xs),
          Text('Your account at a glance.', style: GpTypography.bodyMedium),
        ],
      );
}
