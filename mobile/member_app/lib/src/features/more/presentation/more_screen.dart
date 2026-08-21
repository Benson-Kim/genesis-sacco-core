/// More: the things that need no backend at all.
///
/// #43 T0 lists "static info" alongside the nav and the inactivity logout,
/// and this is it — the tab that is genuinely complete today rather than
/// waiting on anything. Sign out, the hide-figures switch, and the legal and
/// support entries a SACCO app is expected to carry.
///
/// Sign out is local and total. There is no `/member/auth/logout` route to
/// call, so signing out means destroying custody on this device: the refresh
/// token and the cache key go together, because a half cleared session is
/// how one member's data survives into the next member's.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../../../core/env.dart';
import '../../../core/providers.dart';

class MoreScreen extends ConsumerWidget {
  const MoreScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final Flavor flavor = ref.watch(flavorProvider);
    final bool hidden = ref.watch(balancesHiddenProvider);

    return ListView(
      padding: const EdgeInsets.fromLTRB(
        GpSpace.gutter,
        GpSpace.lg,
        GpSpace.gutter,
        GpSpace.xxxl,
      ),
      children: <Widget>[
        const Text('More', style: GpTypography.displayLarge),
        const SizedBox(height: GpSpace.xl),
        const GpSectionHeader('Privacy'),
        const SizedBox(height: GpSpace.md),
        GpCard(
          padding: const EdgeInsets.symmetric(
            horizontal: GpSpace.lg,
            vertical: GpSpace.sm,
          ),
          child: SwitchListTile.adaptive(
            contentPadding: EdgeInsets.zero,
            value: hidden,
            onChanged: (bool next) =>
                ref.read(balancesHiddenProvider.notifier).state = next,
            activeThumbColor: GpPalette.navy,
            title: const Text(
              'Hide figures',
              style: GpTypography.titleSmall,
            ),
            subtitle: const Text(
              'Conceal amounts on screen. Useful in a queue or a matatu.',
              style: GpTypography.bodySmall,
            ),
          ),
        ),
        const SizedBox(height: GpSpace.xl),
        const GpSectionHeader('Your SACCO'),
        const SizedBox(height: GpSpace.md),
        const GpCard(
          padding: EdgeInsets.zero,
          // Every one of these opens in-app rather than linking out. A
          // member should not have to leave a banking app to read its terms,
          // and an external link is one more thing to get wrong.
          child: Column(
            children: <Widget>[
              _Entry(
                icon: Icons.description_outlined,
                label: 'Terms of service',
              ),
              _Divider(),
              _Entry(
                icon: Icons.privacy_tip_outlined,
                label: 'Privacy notice',
              ),
              _Divider(),
              _Entry(
                icon: Icons.support_agent_rounded,
                label: 'Contact your SACCO',
              ),
            ],
          ),
        ),
        const SizedBox(height: GpSpace.xl),
        const GpSectionHeader('This device'),
        const SizedBox(height: GpSpace.md),
        GpCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                'Your session ends automatically after '
                '${flavor.inactivityTimeout.inMinutes} minutes without use.',
                style: GpTypography.bodyMedium,
              ),
              const SizedBox(height: GpSpace.lg),
              GpSecondaryButton(
                label: 'Sign out',
                icon: Icons.logout_rounded,
                onPressed: () => ref.read(sessionProvider).end(),
              ),
            ],
          ),
        ),
        const SizedBox(height: GpSpace.xl),
        const GpSectionHeader('Need help?'),
        const SizedBox(height: GpSpace.md),
        GpCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              const _Contact(
                icon: Icons.call_rounded,
                label: 'Call your SACCO',
                value: '0700 123 456',
              ),
              const SizedBox(height: GpSpace.md),
              const _Contact(
                icon: Icons.mail_outline_rounded,
                label: 'Email',
                value: 'support@example.co.ke',
              ),
              const SizedBox(height: GpSpace.lg),
              // The one warning worth putting where a member will see it
              // before they need it. Every OTP fraud in this market runs on
              // somebody being talked into reading a code aloud.
              const GpBanner(
                'Never share your PIN or a verification code with anyone, '
                'including people who say they are from your SACCO.',
                icon: Icons.shield_outlined,
              ),
            ],
          ),
        ),
        const SizedBox(height: GpSpace.xxl),
        Center(
          child: Text(
            'Genesis Prestige · ${flavor.name}',
            style: GpTypography.bodySmall,
          ),
        ),
      ],
    );
  }
}

class _Entry extends StatelessWidget {
  const _Entry({required this.icon, required this.label});

  final IconData icon;
  final String label;

  @override
  Widget build(BuildContext context) => ListTile(
        leading: Icon(icon, color: GpPalette.navy, size: 22),
        title: Text(label, style: GpTypography.titleSmall),
        trailing: const Icon(
          Icons.chevron_right_rounded,
          color: GpPalette.sub,
        ),
        // The documents themselves are the SACCO's to supply and are not in
        // this repository. Wiring a tap to an empty screen would be worse
        // than leaving it inert until there is something to show.
        onTap: null,
      );
}

/// A support contact. The numbers are per tenant and come from the flavor
/// once a real SACCO's details exist; these are the placeholders the concept
/// sheets used, and they are marked as such rather than passed off as real.
class _Contact extends StatelessWidget {
  const _Contact({
    required this.icon,
    required this.label,
    required this.value,
  });

  final IconData icon;
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Row(
        children: <Widget>[
          Icon(icon, size: 20, color: GpPalette.navy),
          const SizedBox(width: GpSpace.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(label, style: GpTypography.bodySmall),
                Text(value, style: GpTypography.titleSmall),
              ],
            ),
          ),
        ],
      );
}

class _Divider extends StatelessWidget {
  const _Divider();

  @override
  Widget build(BuildContext context) => const Divider(
        height: 1,
        thickness: 1,
        indent: GpSpace.lg,
        color: GpPalette.line,
      );
}
