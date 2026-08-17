import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';
import '../../../features/auth/data/auth_notifier.dart';
import '../../../core/providers.dart';

/// Admin dashboard — authenticated shell screen.
/// Full feature set built in P18.
class DashboardScreen extends ConsumerWidget {
  const DashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final permissions = ref.watch(permissionsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Admin Dashboard'),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout_rounded),
            tooltip: 'Sign out',
            onPressed: () =>
                ref.read(authNotifierProvider.notifier).logout(),
          ),
        ],
      ),
      body: _DashboardBody(permissions: permissions),
    );
  }
}

class _DashboardBody extends StatelessWidget {
  const _DashboardBody({required this.permissions});

  final List<String> permissions;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        // Scaffold placeholders — real data wired in P18.
        const GpStatCard(label: 'Total Members', value: '—'),
        const SizedBox(height: 12),
        const GpStatCard(label: 'Active Loans', value: '—'),
        const SizedBox(height: 12),
        const GpStatCard(label: 'Portfolio at Risk (PAR-30)', value: '—%'),
        const SizedBox(height: 32),
        const Text('Modules', style: GpTypography.titleMedium),
        const SizedBox(height: 12),
        _ModuleGrid(permissions: permissions),
      ],
    );
  }
}

class _ModuleGrid extends StatelessWidget {
  const _ModuleGrid({required this.permissions});

  final List<String> permissions;

  @override
  Widget build(BuildContext context) {
    // Module tiles — visibility gated by RBAC permissions in P18.
    final modules = [
      (Icons.people_outline_rounded, 'Members'),
      (Icons.assignment_outlined, 'Applications'),
      (Icons.account_balance_outlined, 'Loan Book'),
      (Icons.handshake_outlined, 'Guarantors'),
      (Icons.receipt_long_outlined, 'Transactions'),
      (Icons.exit_to_app_rounded, 'Member Exit'),
      (Icons.bar_chart_rounded, 'Reports'),
      (Icons.settings_outlined, 'Settings'),
    ];

    return Wrap(
      spacing: 12,
      runSpacing: 12,
      children: modules
          .map((m) => _ModuleTile(icon: m.$1, label: m.$2))
          .toList(),
    );
  }
}

class _ModuleTile extends StatelessWidget {
  const _ModuleTile({required this.icon, required this.label});

  final IconData icon;
  final String label;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: (MediaQuery.of(context).size.width - 44) / 2,
      child: InkWell(
        onTap: () {}, // wired in P18
        borderRadius: BorderRadius.circular(12),
        child: Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: GpPalette.card,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: GpPalette.line),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(icon, color: GpPalette.navy, size: 28),
              const SizedBox(height: 8),
              Text(label, style: GpTypography.titleMedium),
            ],
          ),
        ),
      ),
    );
  }
}
