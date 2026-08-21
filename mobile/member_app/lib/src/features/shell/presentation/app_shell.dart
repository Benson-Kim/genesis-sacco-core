/// The signed-in shell: five tabs, most of them locked.
///
/// Review §3.2 and #43 T0 fix the shape — Home, Accounts, Transact, Loans,
/// More — and the capability map fixes which of them a member can reach.
/// Today that is Home and More, because no read endpoint is merged.
///
/// # Why locked tabs are shown at all
///
/// The alternative is a bar that grows from two items to five over a few
/// releases, moving every destination under the member's thumb each time.
/// Muscle memory is the thing a bottom bar is FOR; rearranging it is worse
/// than showing a member something they cannot open yet. So the bar keeps its
/// final shape from the first release, and the tabs that are not ready say
/// so.
///
/// The lock is enforced twice, deliberately. [GpBottomNav] never wires a
/// callback for a locked destination, so it cannot fire; and [_select]
/// re-checks before switching, so a future caller reaching the tab another
/// way — a deep link, a programmatic jump — hits the same rule rather than a
/// half-built screen.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../../../core/env.dart';
import '../../../core/providers.dart';
import '../../home/presentation/home_screen.dart';
import '../../more/presentation/more_screen.dart';
import 'not_yet_tab.dart';

class AppShell extends ConsumerStatefulWidget {
  const AppShell({super.key, this.initialTab = 0});

  /// Which tab to open on.
  ///
  /// Exists so a golden can capture a tab other than Home. The alternative
  /// was a test that reached into the bottom bar and invoked a callback by
  /// index, which would have been a test asserting things about its own
  /// cleverness rather than about the screen.
  final int initialTab;

  @override
  ConsumerState<AppShell> createState() => _AppShellState();
}

class _AppShellState extends ConsumerState<AppShell> {
  late int _index = widget.initialTab;

  @override
  Widget build(BuildContext context) {
    final Capabilities capabilities = ref.watch(flavorProvider).capabilities;

    final List<GpNavDestination> destinations = <GpNavDestination>[
      const GpNavDestination(
        icon: Icons.home_outlined,
        selectedIcon: Icons.home_rounded,
        label: 'Home',
      ),
      GpNavDestination(
        icon: Icons.account_balance_wallet_outlined,
        selectedIcon: Icons.account_balance_wallet_rounded,
        label: 'Accounts',
        locked: !capabilities.balances,
      ),
      GpNavDestination(
        icon: Icons.swap_vert_rounded,
        selectedIcon: Icons.swap_vert_rounded,
        label: 'Transact',
        locked: !capabilities.deposits,
      ),
      GpNavDestination(
        icon: Icons.request_quote_outlined,
        selectedIcon: Icons.request_quote_rounded,
        label: 'Loans',
        locked: !capabilities.loans,
      ),
      const GpNavDestination(
        icon: Icons.more_horiz_rounded,
        selectedIcon: Icons.more_horiz_rounded,
        label: 'More',
      ),
    ];

    return Scaffold(
      backgroundColor: GpPalette.bg,
      body: SafeArea(
        bottom: false,
        // IndexedStack rather than a swapped child: Home keeps its scroll
        // position when a member checks something under More and comes back.
        child: IndexedStack(
          index: _index,
          children: const <Widget>[
            HomeScreen(),
            NotYetTab(
              title: 'Accounts',
              headline: 'Your accounts are almost here',
              message: 'Savings, shares and account balances will appear on '
                  'this tab as soon as your SACCO switches them on.',
              icon: Icons.account_balance_wallet_rounded,
            ),
            NotYetTab(
              title: 'Transact',
              headline: 'Deposits and transfers are coming',
              message: 'You will be able to pay in, move funds between your '
                  'accounts and repay a loan from here.',
              icon: Icons.swap_vert_rounded,
            ),
            NotYetTab(
              title: 'Loans',
              headline: 'Your loans are coming',
              message: 'Balances, repayment schedules and applications will '
                  'appear on this tab.',
              icon: Icons.request_quote_rounded,
            ),
            MoreScreen(),
          ],
        ),
      ),
      bottomNavigationBar: GpBottomNav(
        destinations: destinations,
        currentIndex: _index,
        onSelected: (int next) => _select(next, destinations),
      ),
    );
  }

  void _select(int next, List<GpNavDestination> destinations) {
    // The second enforcement. GpBottomNav already refuses to fire for a
    // locked destination; this refuses to honour it even if something else
    // does, so #43's rule does not rest on one widget being correct.
    if (destinations[next].locked) {
      return;
    }
    setState(() => _index = next);
  }
}
