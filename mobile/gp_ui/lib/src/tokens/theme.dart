import 'package:flutter/material.dart';
import 'palette.dart';
import 'typography.dart';

/// Genesis Prestige [ThemeData] built from palette tokens.
abstract final class GpTheme {
  static ThemeData get light => ThemeData(
        useMaterial3: true,
        colorScheme: const ColorScheme(
          brightness: Brightness.light,
          primary: GpPalette.navy,
          onPrimary: Colors.white,
          primaryContainer: GpPalette.navySoft,
          onPrimaryContainer: GpPalette.navy,
          secondary: GpPalette.gold,
          onSecondary: GpPalette.navy,
          secondaryContainer: GpPalette.goldSoft,
          onSecondaryContainer: GpPalette.navy,
          tertiary: GpPalette.emerald,
          onTertiary: Colors.white,
          tertiaryContainer: GpPalette.emeraldSoft,
          onTertiaryContainer: GpPalette.emerald,
          error: GpPalette.brick,
          onError: Colors.white,
          errorContainer: GpPalette.brickSoft,
          onErrorContainer: GpPalette.brick,
          surface: GpPalette.card,
          onSurface: GpPalette.ink,
          surfaceContainerHighest: GpPalette.panel,
          onSurfaceVariant: GpPalette.sub,
          outline: GpPalette.line,
          outlineVariant: GpPalette.line,
          shadow: GpPalette.ink,
          scrim: GpPalette.ink,
          inverseSurface: GpPalette.ink,
          onInverseSurface: Colors.white,
          inversePrimary: GpPalette.steel,
        ),
        scaffoldBackgroundColor: GpPalette.bg,
        cardColor: GpPalette.card,
        dividerColor: GpPalette.line,
        textTheme: const TextTheme(
          displayLarge: GpTypography.displayLarge,
          headlineMedium: GpTypography.headlineMedium,
          titleMedium: GpTypography.titleMedium,
          bodyLarge: GpTypography.bodyLarge,
          bodyMedium: GpTypography.bodyMedium,
          labelSmall: GpTypography.labelSmall,
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: GpPalette.navy,
          foregroundColor: Colors.white,
          elevation: 0,
          centerTitle: false,
        ),
        elevatedButtonTheme: ElevatedButtonThemeData(
          style: ElevatedButton.styleFrom(
            backgroundColor: GpPalette.navy,
            foregroundColor: Colors.white,
            minimumSize: const Size.fromHeight(48),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(8),
            ),
          ),
        ),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: GpPalette.card,
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: GpPalette.line),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: GpPalette.line),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: GpPalette.navy, width: 2),
          ),
        ),
      );
}
