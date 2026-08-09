import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';
import '../../../features/auth/data/auth_notifier.dart';

/// Member dashboard — authenticated shell screen.
/// Displays balance summary cards (populated in P17).
class DashboardScreen extends ConsumerWidget {
  const DashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('My Account'),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout_rounded),
            tooltip: 'Sign out',
            onPressed: () => ref.read(authNotifierProvider.notifier).logout(),
          ),
        ],
      ),
      body: const _DashboardBody(),
    );
  }
}

class _DashboardBody extends StatelessWidget {
  const _DashboardBody();

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: const [
        // Scaffold placeholders — real data wired in P17.
        GpStatCard(
          label: 'Share Balance',
          value: '— KES',
        ),
        SizedBox(height: 12),
        GpStatCard(
          label: 'Deposit Balance',
          value: '— KES',
        ),
        SizedBox(height: 12),
        GpStatCard(
          label: 'Loan Balance',
          value: '— KES',
        ),
        SizedBox(height: 32),
        _QuickActions(),
      ],
    );
  }
}

class _QuickActions extends StatelessWidget {
  const _QuickActions();

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('Quick Actions', style: GpTypography.titleMedium),
        const SizedBox(height: 12),
        Wrap(
          spacing: 12,
          runSpacing: 12,
          children: [
            _ActionChip(
              icon: Icons.account_balance_wallet_outlined,
              label: 'Deposit',
              onTap: () {}, // wired in P17
            ),
            _ActionChip(
              icon: Icons.receipt_long_outlined,
              label: 'Statement',
              onTap: () {}, // wired in P17
            ),
            _ActionChip(
              icon: Icons.credit_score_outlined,
              label: 'Apply for Loan',
              onTap: () {}, // wired in P17
            ),
          ],
        ),
      ],
    );
  }
}

class _ActionChip extends StatelessWidget {
  const _ActionChip({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(12),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        decoration: BoxDecoration(
          color: GpPalette.navySoft,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: GpPalette.line),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 18, color: GpPalette.navy),
            const SizedBox(width: 8),
            Text(label,
                style: GpTypography.bodyMedium.copyWith(color: GpPalette.navy)),
          ],
        ),
      ),
    );
  }
}
