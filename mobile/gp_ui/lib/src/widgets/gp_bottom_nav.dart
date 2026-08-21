/// The five item bottom bar (#43 T0, review §3.2).
///
/// Hand built rather than Material's `NavigationBar`, for one reason that
/// matters: destinations here can be LOCKED, and Material's bar has no such
/// state. Its `NavigationDestination` is either present and selectable or
/// absent, so representing "this tab exists and is coming" would mean either
/// removing the tab — hiding the roadmap and making the bar's item count
/// change between releases — or leaving it live and letting a member tap into
/// a screen with nothing behind it, which is exactly what #43 forbids.
///
/// A locked destination is dimmed, marked, and inert: [onSelected] is not
/// called for it, so no caller can accidentally route to it.
library;

import 'package:flutter/material.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

@immutable
class GpNavDestination {
  const GpNavDestination({
    required this.icon,
    required this.selectedIcon,
    required this.label,
    this.locked = false,
  });

  final IconData icon;
  final IconData selectedIcon;
  final String label;

  /// From the build time capability map. A locked tab still occupies its
  /// place, so the bar does not reshuffle under a member's thumb when a
  /// feature ships.
  final bool locked;
}

class GpBottomNav extends StatelessWidget {
  const GpBottomNav({
    required this.destinations,
    required this.currentIndex,
    required this.onSelected,
    super.key,
  });

  final List<GpNavDestination> destinations;
  final int currentIndex;
  final ValueChanged<int> onSelected;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: const BoxDecoration(
        color: GpPalette.card,
        border: Border(top: BorderSide(color: GpPalette.line)),
      ),
      child: SafeArea(
        top: false,
        child: SizedBox(
          height: 64,
          child: Row(
            children: <Widget>[
              for (int i = 0; i < destinations.length; i++)
                Expanded(
                  child: _Item(
                    destination: destinations[i],
                    selected: i == currentIndex,
                    // A locked destination is not merely styled as inert, it
                    // IS inert: the callback is never wired up.
                    onTap: destinations[i].locked ? null : () => onSelected(i),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _Item extends StatelessWidget {
  const _Item({
    required this.destination,
    required this.selected,
    required this.onTap,
  });

  final GpNavDestination destination;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final bool locked = destination.locked;
    final Color color = locked
        ? GpPalette.sub.withValues(alpha: 0.45)
        : (selected ? GpPalette.navy : GpPalette.sub);

    return Semantics(
      selected: selected,
      enabled: !locked,
      button: true,
      label: locked
          ? '${destination.label}, not available yet'
          : destination.label,
      child: InkWell(
        onTap: onTap,
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            Icon(
              selected ? destination.selectedIcon : destination.icon,
              size: 24,
              color: color,
            ),
            const SizedBox(height: GpSpace.xs),
            Text(
              destination.label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: GpTypography.bodySmall.copyWith(
                fontSize: 11,
                color: color,
                fontWeight: selected ? FontWeight.w800 : FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
