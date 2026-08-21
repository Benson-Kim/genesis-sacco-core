/// A quick action tile, and the locked state that is most of its job.
///
/// #43's rule: "a Favorites tile must never launch an unbuilt flow", and tier
/// state is a build time capability map rather than a runtime probe. Every
/// tile in the app is currently locked, because no flow behind one has a
/// merged endpoint.
///
/// A locked tile is inert AND says why. Three things are deliberate:
///
/// * `onTap` is ignored entirely when [locked] — the tile cannot fire even if
///   a caller passes a callback, so the guarantee does not depend on every
///   call site remembering to pass null.
/// * It is dimmed but still legible. Greying a control into near invisibility
///   hides the roadmap; the point of showing these at all is that a member
///   can see what the SACCO is going to offer.
/// * It carries a caption, and the caption is not a promise. "Soon" with no
///   date is honest. A date the backend has not committed to is not.
library;

import 'package:flutter/material.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

class GpActionTile extends StatelessWidget {
  const GpActionTile({
    required this.icon,
    required this.label,
    super.key,
    this.onTap,
    this.locked = false,
    this.lockedCaption = 'Soon',
  });

  final IconData icon;
  final String label;
  final VoidCallback? onTap;

  /// Set from the build time capability map, never from a server response.
  final bool locked;

  final String lockedCaption;

  @override
  Widget build(BuildContext context) {
    final Color foreground = locked ? GpPalette.sub : GpPalette.navy;
    final Color tileColor = locked ? GpPalette.panel : GpPalette.navySoft;

    final Widget tile = Column(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Opacity(
          opacity: locked ? 0.55 : 1,
          child: Container(
            width: 56,
            height: 56,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: tileColor,
              borderRadius: BorderRadius.circular(GpRadius.control),
            ),
            child: Icon(icon, size: 24, color: foreground),
          ),
        ),
        const SizedBox(height: GpSpace.sm),
        Text(
          label,
          textAlign: TextAlign.center,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          style: GpTypography.labelSmall.copyWith(
            color: locked ? GpPalette.sub : GpPalette.ink,
            fontWeight: FontWeight.w700,
          ),
        ),
        if (locked) ...<Widget>[
          const SizedBox(height: 2),
          Text(
            lockedCaption,
            textAlign: TextAlign.center,
            style: GpTypography.bodySmall.copyWith(
              fontSize: 11,
              color: GpPalette.sub.withValues(alpha: 0.8),
            ),
          ),
        ],
      ],
    );

    return Semantics(
      button: true,
      enabled: !locked,
      label: locked ? '$label, not available yet' : label,
      child: SizedBox(
        width: 80,
        child: locked
            ? tile
            : Material(
                color: Colors.transparent,
                child: InkWell(
                  onTap: onTap,
                  borderRadius: BorderRadius.circular(GpRadius.control),
                  child: tile,
                ),
              ),
      ),
    );
  }
}
