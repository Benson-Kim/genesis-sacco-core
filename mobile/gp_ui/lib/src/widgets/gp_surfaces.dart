/// The surfaces everything else sits on: the card, the section rule, and the
/// banner.
///
/// The prototype is a border design, not a shadow design — `.card` is a 1px
/// `--line` border on white and nothing more. Keeping that is most of what
/// makes the mobile app look like the same product as the console, so these
/// three carry over almost unchanged.
library;

import 'package:flutter/material.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// The prototype's `.card`: white, one hairline border, 16px corners.
class GpCard extends StatelessWidget {
  const GpCard({
    required this.child,
    super.key,
    this.padding = const EdgeInsets.all(GpSpace.cardPadding),
    this.color = GpPalette.card,
    this.onTap,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final Color color;

  /// When set, the whole card becomes one target — which is the only
  /// tappable-card shape allowed here. A card with a small link buried in it
  /// is a card most people will miss on a phone.
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final Widget content = Padding(padding: padding, child: child);
    // A Material rather than a DecoratedBox, even when nothing is tappable.
    // ListTile and every ink effect paint on the nearest Material ancestor,
    // so a plain DecoratedBox in between swallows them — and Flutter says so
    // by asserting, which is how this was found: a card holding a
    // SwitchListTile threw "background color or ink splashes may be
    // invisible" the first time one was rendered.
    return Material(
      color: color,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(GpRadius.card),
        side: const BorderSide(color: GpPalette.line),
      ),
      child: onTap == null
          ? content
          : InkWell(onTap: onTap, child: content),
    );
  }
}

/// The prototype's `.subhead`: an uppercase navy label over a hairline.
///
/// On mobile the rule below the text is dropped when the section is a list of
/// cards — the cards already draw their own edges, and two borders 8px apart
/// is noise. It is kept for sections of plain rows.
class GpSectionHeader extends StatelessWidget {
  const GpSectionHeader(
    this.label, {
    super.key,
    this.trailing,
    this.underline = false,
  });

  final String label;

  /// A single action, at most. Section headers are not toolbars.
  final Widget? trailing;
  final bool underline;

  @override
  Widget build(BuildContext context) {
    final Widget row = Row(
      children: <Widget>[
        Expanded(
          child: Text(label.toUpperCase(), style: GpTypography.sectionHeader),
        ),
        if (trailing != null) trailing!,
      ],
    );
    if (!underline) {
      return row;
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        row,
        const SizedBox(height: GpSpace.sm),
        const Divider(height: 1, thickness: 1, color: GpPalette.line),
      ],
    );
  }
}

/// What a banner is saying.
enum GpBannerTone { info, positive, warning, danger }

/// The prototype's `.banner`: a tinted strip carrying one sentence.
///
/// Used for the staleness marker on cached reads (FM-C requires a cached
/// screen to say so, explicitly, rather than pass for live) and for anything
/// else that qualifies what is on screen without being an error.
class GpBanner extends StatelessWidget {
  const GpBanner(
    this.message, {
    super.key,
    this.tone = GpBannerTone.info,
    this.icon,
  });

  final String message;
  final GpBannerTone tone;
  final IconData? icon;

  ({Color background, Color foreground}) get _colors {
    switch (tone) {
      case GpBannerTone.info:
        return (
          background: GpPalette.navySoft,
          foreground: GpPalette.navy,
        );
      case GpBannerTone.positive:
        return (
          background: GpPalette.emeraldSoft,
          foreground: GpPalette.emerald,
        );
      case GpBannerTone.warning:
        return (
          background: GpPalette.orangeSoft,
          foreground: GpPalette.orange,
        );
      case GpBannerTone.danger:
        return (
          background: GpPalette.brickSoft,
          foreground: GpPalette.brick,
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    final ({Color background, Color foreground}) colors = _colors;
    return Container(
      padding: const EdgeInsets.symmetric(
        horizontal: GpSpace.md,
        vertical: GpSpace.md,
      ),
      decoration: BoxDecoration(
        color: colors.background,
        borderRadius: BorderRadius.circular(GpRadius.control),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          if (icon != null) ...<Widget>[
            Icon(icon, size: 18, color: colors.foreground),
            const SizedBox(width: GpSpace.sm),
          ],
          Expanded(
            child: Text(
              message,
              style: GpTypography.bodySmall.copyWith(
                color: colors.foreground,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
