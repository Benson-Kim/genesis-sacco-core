/// Empty and not-yet states.
///
/// There are two of these and they are not the same thing, which is why they
/// are two:
///
/// * [GpEmptyState] — the query worked and there is nothing to show. "No
///   transactions this month."
/// * [GpNotYetState] — the feature is not built. Nothing was asked of any
///   server, and nothing will be until the endpoint behind it merges.
///
/// Collapsing them into one "empty" component is how an app ends up telling a
/// member they have no loans when the truth is that nobody has shipped the
/// loans screen yet. Those two sentences have very different consequences for
/// somebody deciding whether to trust the SACCO with their money.
library;

import 'package:flutter/material.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// The query succeeded and returned nothing.
class GpEmptyState extends StatelessWidget {
  const GpEmptyState({
    required this.title,
    super.key,
    this.message,
    this.icon = Icons.inbox_rounded,
  });

  final String title;
  final String? message;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(
        horizontal: GpSpace.xl,
        vertical: GpSpace.xxxl,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Icon(icon, size: 32, color: GpPalette.sub.withValues(alpha: 0.55)),
          const SizedBox(height: GpSpace.md),
          Text(
            title,
            textAlign: TextAlign.center,
            style: GpTypography.titleSmall,
          ),
          if (message != null) ...<Widget>[
            const SizedBox(height: GpSpace.xs),
            Text(
              message!,
              textAlign: TextAlign.center,
              style: GpTypography.bodyMedium,
            ),
          ],
        ],
      ),
    );
  }
}

/// The feature is not built yet, and the screen says so without pretending.
///
/// No retry button: there is nothing to retry. Offering one would invite a
/// member to tap it repeatedly at a wall.
class GpNotYetState extends StatelessWidget {
  const GpNotYetState({
    required this.title,
    required this.message,
    super.key,
    this.icon = Icons.schedule_rounded,
    this.action,
  });

  final String title;

  /// Says what will be here, in the member's terms. Not "endpoint unmerged".
  final String message;

  final IconData icon;

  /// Somewhere genuinely useful to go instead, when there is one.
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(
        horizontal: GpSpace.xl,
        vertical: GpSpace.xxxl,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Container(
            width: 64,
            height: 64,
            alignment: Alignment.center,
            decoration: const BoxDecoration(
              color: GpPalette.navySoft,
              shape: BoxShape.circle,
            ),
            child: Icon(icon, size: 28, color: GpPalette.navy),
          ),
          const SizedBox(height: GpSpace.lg),
          Text(
            title,
            textAlign: TextAlign.center,
            style: GpTypography.titleMedium,
          ),
          const SizedBox(height: GpSpace.sm),
          Text(
            message,
            textAlign: TextAlign.center,
            style: GpTypography.bodyMedium,
          ),
          if (action != null) ...<Widget>[
            const SizedBox(height: GpSpace.xl),
            action!,
          ],
        ],
      ),
    );
  }
}
