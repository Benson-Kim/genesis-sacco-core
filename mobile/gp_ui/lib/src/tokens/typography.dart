import 'package:flutter/material.dart';
import 'palette.dart';

/// Typography scale for Genesis Prestige apps.
abstract final class GpTypography {
  static const TextStyle displayLarge = TextStyle(
    fontSize: 28,
    fontWeight: FontWeight.w700,
    color: GpPalette.ink,
    letterSpacing: -0.5,
  );

  static const TextStyle headlineMedium = TextStyle(
    fontSize: 20,
    fontWeight: FontWeight.w600,
    color: GpPalette.ink,
  );

  static const TextStyle titleMedium = TextStyle(
    fontSize: 16,
    fontWeight: FontWeight.w600,
    color: GpPalette.ink,
  );

  static const TextStyle bodyLarge = TextStyle(
    fontSize: 15,
    fontWeight: FontWeight.w400,
    color: GpPalette.ink,
  );

  static const TextStyle bodyMedium = TextStyle(
    fontSize: 13,
    fontWeight: FontWeight.w400,
    color: GpPalette.sub,
  );

  static const TextStyle labelSmall = TextStyle(
    fontSize: 11,
    fontWeight: FontWeight.w500,
    color: GpPalette.sub,
    letterSpacing: 0.4,
  );

  static const TextStyle moneyLarge = TextStyle(
    fontSize: 24,
    fontWeight: FontWeight.w700,
    color: GpPalette.ink,
    fontFeatures: [FontFeature.tabularFigures()],
  );
}
