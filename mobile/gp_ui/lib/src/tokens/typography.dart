import 'package:flutter/material.dart';
import 'palette.dart';

/// Typography scale for Genesis Prestige apps.
///
/// # Retuned for a phone
///
/// The prototype sets body copy at 13px and labels at 11.5px. Those are
/// desktop sizes, read at desk distance on a large display, and carrying them
/// to a phone would produce an app that is technically consistent and
/// practically unreadable. Body copy here is 15/14 and the smallest label is
/// 12 — the floor below which text stops being text on a handset.
///
/// The WEIGHTS are the prototype's, because they are what the design actually
/// sounds like: 800 for anything numeric or structural, 700 for emphasis, 400
/// for prose. The prototype almost never uses 500 or 600, and neither does
/// this.
///
/// # No bundled font
///
/// Nothing here names a font family, so every style resolves to the platform
/// default: San Francisco on iOS, Roboto on Android. That is the prototype's
/// own rule (`-apple-system,'Segoe UI',Roboto,system-ui`), and it means the
/// app ships no font payload, renders with no first-paint swap, and inherits
/// the reader's system font scaling for free. Nothing is exempt, [brandMark]
/// included — see the note there for what happened when it was.
abstract final class GpTypography {
  /// Numbers are ALWAYS tabular.
  ///
  /// Money that shifts sideways as digits change is money the eye cannot
  /// compare down a column, and every money surface in this app is a column.
  /// The prototype's `.tnum` class, made non-optional.
  static const List<FontFeature> _tabular = <FontFeature>[
    FontFeature.tabularFigures(),
  ];

  // ── Display and headings ────────────────────────────────────────────────

  /// Screen titles. One per screen, at most.
  static const TextStyle displayLarge = TextStyle(
    fontSize: 28,
    fontWeight: FontWeight.w800,
    color: GpPalette.ink,
    letterSpacing: -0.5,
    height: 1.15,
  );

  /// Card and sheet titles — the prototype's gate heading at 20/800.
  static const TextStyle headlineMedium = TextStyle(
    fontSize: 20,
    fontWeight: FontWeight.w800,
    color: GpPalette.ink,
    letterSpacing: -0.2,
    height: 1.2,
  );

  /// `h2.sec-t` — a section title inside a card.
  static const TextStyle titleMedium = TextStyle(
    fontSize: 16,
    fontWeight: FontWeight.w700,
    color: GpPalette.ink,
    height: 1.25,
  );

  /// A row title in a list.
  static const TextStyle titleSmall = TextStyle(
    fontSize: 15,
    fontWeight: FontWeight.w700,
    color: GpPalette.ink,
    height: 1.3,
  );

  /// `.subhead` — the uppercase rule-line above a section. Verbatim from the
  /// prototype apart from the size, which cannot go below 12 on a phone.
  static const TextStyle sectionHeader = TextStyle(
    fontSize: 12,
    fontWeight: FontWeight.w800,
    color: GpPalette.navyMid,
    letterSpacing: 0.8,
    height: 1.2,
  );

  // ── Body ────────────────────────────────────────────────────────────────

  static const TextStyle bodyLarge = TextStyle(
    fontSize: 15,
    fontWeight: FontWeight.w400,
    color: GpPalette.ink,
    height: 1.45,
  );

  /// Secondary prose. 14, not the prototype's 13.
  static const TextStyle bodyMedium = TextStyle(
    fontSize: 14,
    fontWeight: FontWeight.w400,
    color: GpPalette.sub,
    height: 1.45,
  );

  /// The smallest text that ships. Captions, footnotes, timestamps.
  static const TextStyle bodySmall = TextStyle(
    fontSize: 12,
    fontWeight: FontWeight.w400,
    color: GpPalette.sub,
    height: 1.4,
  );

  static const TextStyle labelSmall = TextStyle(
    fontSize: 12,
    fontWeight: FontWeight.w600,
    color: GpPalette.sub,
    letterSpacing: 0.3,
    height: 1.3,
  );

  /// Inside a pill or chip.
  static const TextStyle pillLabel = TextStyle(
    fontSize: 12,
    fontWeight: FontWeight.w700,
    letterSpacing: 0.2,
    height: 1.1,
  );

  /// On a button. The prototype's 800 weight, at a size a thumb aims for.
  static const TextStyle buttonLabel = TextStyle(
    fontSize: 15,
    fontWeight: FontWeight.w800,
    letterSpacing: 0.2,
    height: 1.2,
  );

  // ── Money ───────────────────────────────────────────────────────────────

  /// The one number on the home screen. White, because it sits on the navy
  /// hero; callers restyle the colour for other grounds.
  static const TextStyle moneyHero = TextStyle(
    fontSize: 32,
    fontWeight: FontWeight.w800,
    color: Colors.white,
    letterSpacing: -0.8,
    height: 1.1,
    fontFeatures: _tabular,
  );

  static const TextStyle moneyLarge = TextStyle(
    fontSize: 24,
    fontWeight: FontWeight.w800,
    color: GpPalette.ink,
    letterSpacing: -0.4,
    height: 1.15,
    fontFeatures: _tabular,
  );

  /// A balance in a list row or a secondary tile.
  static const TextStyle moneyMedium = TextStyle(
    fontSize: 17,
    fontWeight: FontWeight.w800,
    color: GpPalette.ink,
    height: 1.2,
    fontFeatures: _tabular,
  );

  /// The `KES` prefix. Deliberately quieter than the figure it introduces —
  /// the currency is context, the amount is information.
  static const TextStyle currencyPrefix = TextStyle(
    fontSize: 13,
    fontWeight: FontWeight.w700,
    letterSpacing: 0.5,
    height: 1.2,
  );

  // ── Brand ───────────────────────────────────────────────────────────────

  /// The single letter inside the gold square.
  ///
  /// This named Georgia, with a serif fallback chain, on the reasoning that
  /// the prototype sets its mark in Georgia and a letterform in a square
  /// survives substitution. The first golden render disproved it: no Georgia,
  /// no Times, no serif in the container, so the glyph fell through to notdef
  /// and the brand mark came out as a featureless filled box.
  ///
  /// It could be made to work by bundling a serif, but bundling a font for
  /// ONE GLYPH is a poor trade, and every device where the fallback silently
  /// differs is a device whose mark nobody has looked at. The default face,
  /// heavy and tight, renders identically everywhere including in review.
  static const TextStyle brandMark = TextStyle(
    fontSize: 22,
    fontWeight: FontWeight.w800,
    color: GpPalette.navy,
    letterSpacing: -0.5,
    height: 1.1,
  );

  /// "Genesis Prestige". Sans on mobile, unlike the prototype's Georgia
  /// wordmark: a serif wordmark that silently becomes Roboto on most Android
  /// devices is not a wordmark, it is a bug that ships.
  static const TextStyle brandWordmark = TextStyle(
    fontSize: 17,
    fontWeight: FontWeight.w800,
    color: GpPalette.navy,
    letterSpacing: -0.2,
    height: 1.15,
  );

  /// "ZURI GENESIS · SACCO" — the letterspaced gold line under the wordmark.
  static const TextStyle brandEyebrow = TextStyle(
    fontSize: 10,
    fontWeight: FontWeight.w800,
    color: GpPalette.gold,
    letterSpacing: 1.4,
    height: 1.2,
  );
}
