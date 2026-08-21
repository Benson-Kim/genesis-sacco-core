/// The brand lockup: the gold mark, the wordmark, and the SACCO line.
///
/// Taken from the prototype's login gate, where the same three elements sit
/// together above the sign in form. It is the one place the member app names
/// the co-operative, so it carries the tenant's name rather than a hardcoded
/// one — the app is white labelled per SACCO and the mark is the only piece
/// that is constant.
library;

import 'package:flutter/material.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// The gold rounded square with a letter in it.
///
/// A filled square rather than a bare letterform, deliberately: the glyph is
/// set in a serif the device may not have, so the shape has to survive the
/// substitution. The square does the identifying; the letter decorates it.
class GpBrandMark extends StatelessWidget {
  const GpBrandMark({super.key, this.letter = 'G', this.size = 44});

  final String letter;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: GpPalette.gold,
        borderRadius: BorderRadius.circular(GpRadius.control),
      ),
      child: Text(
        letter,
        style: GpTypography.brandMark.copyWith(fontSize: size * 0.5),
      ),
    );
  }
}

/// Mark, wordmark and eyebrow, in a row.
class GpBrandLockup extends StatelessWidget {
  const GpBrandLockup({
    super.key,
    this.wordmark = 'Genesis Prestige',
    this.eyebrow,
    this.onDark = false,
  });

  final String wordmark;

  /// The tenant line — "ZURI GENESIS · SACCO" in the prototype. Null hides
  /// it rather than showing a placeholder, because a white labelled build
  /// that does not know whose it is should say nothing.
  final String? eyebrow;

  /// Inverts the wordmark for the navy gate background.
  final bool onDark;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        const GpBrandMark(),
        const SizedBox(width: GpSpace.md),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Text(
              wordmark,
              style: onDark
                  ? GpTypography.brandWordmark.copyWith(color: Colors.white)
                  : GpTypography.brandWordmark,
            ),
            if (eyebrow != null) ...<Widget>[
              const SizedBox(height: 2),
              Text(
                eyebrow!.toUpperCase(),
                style: GpTypography.brandEyebrow,
              ),
            ],
          ],
        ),
      ],
    );
  }
}
