/// Buttons and pills.
///
/// Every control here is at least [GpSpace.touchTarget] tall. The prototype's
/// `.btn` computes to about 35px, which is fine under a cursor and below both
/// Material's minimum and WCAG 2.5.5 under a thumb. The border radius, the
/// weight and the colours are the prototype's; the size is not.
///
/// Buttons take a `busy` flag rather than leaving callers to swap in a
/// spinner. In-flight disabling is half of the double submit guard (FM-G),
/// and a guard that each call site re-implements is a guard some call site
/// will forget.
library;

import 'package:flutter/material.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// The prototype's `.btn.p` — solid navy, white label.
class GpPrimaryButton extends StatelessWidget {
  const GpPrimaryButton({
    required this.label,
    required this.onPressed,
    super.key,
    this.busy = false,
    this.busyLabel,
    this.icon,
  });

  final String label;

  /// Null disables the button. [busy] also disables it, without the caller
  /// having to null the callback and lose it.
  final VoidCallback? onPressed;

  final bool busy;

  /// Shown while [busy]. Saying what is happening beats a bare spinner.
  final String? busyLabel;

  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final bool enabled = onPressed != null && !busy;
    return SizedBox(
      width: double.infinity,
      height: GpSpace.touchTarget,
      child: ElevatedButton(
        onPressed: enabled ? onPressed : null,
        style: ElevatedButton.styleFrom(
          backgroundColor: GpPalette.navy,
          foregroundColor: Colors.white,
          disabledBackgroundColor: GpPalette.navy.withValues(alpha: 0.38),
          disabledForegroundColor: Colors.white.withValues(alpha: 0.8),
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(GpRadius.control),
          ),
        ),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            if (busy) ...<Widget>[
              const SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  valueColor: AlwaysStoppedAnimation<Color>(Colors.white),
                ),
              ),
              const SizedBox(width: GpSpace.md),
            ] else if (icon != null) ...<Widget>[
              Icon(icon, size: 18),
              const SizedBox(width: GpSpace.sm),
            ],
            Flexible(
              child: Text(
                busy ? (busyLabel ?? label) : label,
                style: GpTypography.buttonLabel,
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// The prototype's plain `.btn` — white, hairline border, muted label.
class GpSecondaryButton extends StatelessWidget {
  const GpSecondaryButton({
    required this.label,
    required this.onPressed,
    super.key,
    this.icon,
    this.expand = true,
  });

  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;
  final bool expand;

  @override
  Widget build(BuildContext context) {
    final Widget button = SizedBox(
      height: GpSpace.touchTarget,
      child: OutlinedButton(
        onPressed: onPressed,
        style: OutlinedButton.styleFrom(
          foregroundColor: GpPalette.sub,
          side: const BorderSide(color: GpPalette.line, width: 1.5),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(GpRadius.control),
          ),
          padding: const EdgeInsets.symmetric(horizontal: GpSpace.lg),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            if (icon != null) ...<Widget>[
              Icon(icon, size: 18),
              const SizedBox(width: GpSpace.sm),
            ],
            Text(label, style: GpTypography.buttonLabel),
          ],
        ),
      ),
    );
    return expand ? SizedBox(width: double.infinity, child: button) : button;
  }
}

/// A quiet inline action — "Resend", "Use a different number".
class GpTextAction extends StatelessWidget {
  const GpTextAction({
    required this.label,
    required this.onPressed,
    super.key,
    this.emphasis = false,
  });

  final String label;
  final VoidCallback? onPressed;

  /// Navy and bolder. For the one action in a group that is the likely
  /// choice; the prototype uses exactly this contrast for "Resend" beside
  /// "Change number".
  final bool emphasis;

  @override
  Widget build(BuildContext context) {
    return TextButton(
      onPressed: onPressed,
      style: TextButton.styleFrom(
        minimumSize: const Size(0, GpSpace.touchTarget),
        padding: const EdgeInsets.symmetric(horizontal: GpSpace.md),
        foregroundColor: emphasis ? GpPalette.navy : GpPalette.sub,
      ),
      child: Text(
        label,
        style: GpTypography.labelSmall.copyWith(
          fontSize: 14,
          fontWeight: emphasis ? FontWeight.w800 : FontWeight.w700,
          color: emphasis ? GpPalette.navy : GpPalette.sub,
        ),
      ),
    );
  }
}

/// What a pill is reporting.
enum GpPillTone { neutral, positive, warning, danger, brand }

/// The prototype's `.pill` — a fully round status token.
class GpPill extends StatelessWidget {
  const GpPill(this.label, {super.key, this.tone = GpPillTone.neutral});

  final String label;
  final GpPillTone tone;

  ({Color background, Color foreground}) get _colors {
    switch (tone) {
      case GpPillTone.neutral:
        return (background: GpPalette.panel, foreground: GpPalette.sub);
      case GpPillTone.positive:
        return (
          background: GpPalette.emeraldSoft,
          foreground: GpPalette.emerald,
        );
      case GpPillTone.warning:
        return (
          background: GpPalette.orangeSoft,
          foreground: GpPalette.orange,
        );
      case GpPillTone.danger:
        return (background: GpPalette.brickSoft, foreground: GpPalette.brick);
      case GpPillTone.brand:
        return (background: GpPalette.goldSoft, foreground: GpPalette.navy);
    }
  }

  @override
  Widget build(BuildContext context) {
    final ({Color background, Color foreground}) colors = _colors;
    return Container(
      padding: const EdgeInsets.symmetric(
        horizontal: GpSpace.md,
        vertical: 5,
      ),
      decoration: BoxDecoration(
        color: colors.background,
        borderRadius: BorderRadius.circular(GpRadius.pill),
      ),
      child: Text(
        label,
        style: GpTypography.pillLabel.copyWith(color: colors.foreground),
      ),
    );
  }
}
