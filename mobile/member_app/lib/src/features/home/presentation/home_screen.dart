/// The signed-in shell.
///
/// It is nearly empty, and that is the honest state of the platform rather
/// than an unfinished screen. `develop` carries the OTP session and the
/// guarantor consent/release ACTS; it carries no member-audience GET at all.
/// Balances, transactions, statements and loans are all on !7, and the
/// guarantor inbox needs #41 — so `Capabilities.asMergedToday` has every flag
/// false and this screen renders what that actually means.
///
/// #43's rule is that no tile launches an unbuilt flow. The strictest reading
/// of it is that no tile appears at all until its endpoint merges, which is
/// what happens here: the list below is generated FROM the capability map, so
/// the day a flag flips true in the same MR that consumes the merged endpoint,
/// its entry moves from "coming" to a real destination. A hand-written grid of
/// disabled buttons would drift from that map within a release.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../../../core/env.dart';
import '../../../core/providers.dart';

class HomeScreen extends ConsumerWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final Capabilities capabilities = ref.watch(flavorProvider).capabilities;
    final List<String> pending = <String>[
      if (!capabilities.balances) 'Savings and share balances',
      if (!capabilities.transactions) 'Recent transactions',
      if (!capabilities.statement) 'Statements',
      if (!capabilities.loans) 'Loans',
      if (!capabilities.guaranteeInbox) 'Guarantee requests',
      if (!capabilities.deposits) 'Deposits',
      if (!capabilities.loanApplication) 'Loan applications',
    ];

    return Scaffold(
      backgroundColor: GpPalette.bg,
      appBar: AppBar(
        title: const Text('Genesis Prestige'),
        actions: <Widget>[
          IconButton(
            key: const Key('home.signOut'),
            tooltip: 'Sign out',
            icon: const Icon(Icons.logout_rounded),
            // Local, and complete: there is no `/member/auth/logout` route to
            // call, so signing out means destroying custody on this device.
            // end() clears the refresh token and the cache key together — a
            // selective clear is how one member's data survives into the next
            // member's session.
            onPressed: () => ref.read(sessionProvider).end(),
          ),
        ],
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(24),
          children: <Widget>[
            const Text('You are signed in', style: GpTypography.headlineMedium),
            const SizedBox(height: 8),
            const Text(
              'Your session is active on this device and ends automatically '
              'after a few minutes without use.',
              style: GpTypography.bodyMedium,
            ),
            if (pending.isNotEmpty) ...<Widget>[
              const SizedBox(height: 32),
              const Text('Coming next', style: GpTypography.titleMedium),
              const SizedBox(height: 8),
              for (final String item in pending)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Row(
                    children: <Widget>[
                      const Icon(Icons.schedule_rounded,
                          size: 18, color: GpPalette.sub),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Text(item, style: GpTypography.bodyLarge),
                      ),
                    ],
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}
