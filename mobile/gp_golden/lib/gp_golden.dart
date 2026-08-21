/// Golden render harness: real fonts, real device sizes.
///
/// # Why this package exists
///
/// `flutter test` renders text with a placeholder face, so every glyph comes
/// out as a filled rectangle. That is fine when a golden's job is to catch
/// layout regressions, and useless when its job is to let a person look at
/// the design and approve it. A folder of PNGs full of black boxes tells a
/// reviewer nothing at all.
///
/// So [loadGoldenFonts] loads the real Roboto and the Material icon font
/// before anything renders. They are not committed to this repository: they
/// already exist inside the Flutter SDK that CI installs, and shipping copies
/// would put binaries in the tree that can drift from the SDK actually doing
/// the rendering.
///
/// # It fails loudly
///
/// If the fonts cannot be found, [loadGoldenFonts] throws and names every
/// path it looked at along with what it found there. The alternative — quietly
/// carrying on with the placeholder face — produces a green pipeline and a
/// folder of unreadable images, and whoever opens them has to work out why
/// from no evidence.
library gp_golden;

import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

/// The family [loadGoldenFonts] registers text under.
///
/// Nothing in `gp_ui` names a font family — every style resolves to the
/// platform default, which is the right product behaviour and precisely why
/// goldens have to name one explicitly. [goldenTheme] applies it.
const String goldenFontFamily = 'Roboto';

bool _loaded = false;

/// Load Roboto and the Material icon font. Safe to call repeatedly.
Future<void> loadGoldenFonts() async {
  if (_loaded) {
    return;
  }

  final List<Directory> candidates = _candidateFontDirectories();
  Directory? fonts;
  for (final Directory directory in candidates) {
    if (directory.existsSync()) {
      fonts = directory;
      break;
    }
  }
  if (fonts == null) {
    throw StateError(
      'No Flutter material_fonts directory found. Looked in:\n'
      '${candidates.map((Directory d) => '  ${d.path}').join('\n')}\n'
      'FLUTTER_ROOT=${Platform.environment['FLUTTER_ROOT'] ?? '<unset>'}\n'
      'executable=${Platform.resolvedExecutable}',
    );
  }

  final List<File> files = fonts
      .listSync()
      .whereType<File>()
      .where((File f) =>
          f.path.endsWith('.ttf') ||
          f.path.endsWith('.otf') ||
          f.path.endsWith('.ttc'))
      .toList(growable: false);

  final List<File> roboto = files
      .where((File f) => _basename(f).toLowerCase().startsWith('roboto'))
      .toList(growable: false);
  final List<File> icons = files
      .where((File f) => _basename(f).toLowerCase().startsWith('materialicons'))
      .toList(growable: false);

  if (roboto.isEmpty || icons.isEmpty) {
    throw StateError(
      'Expected Roboto and MaterialIcons in ${fonts.path}, found:\n'
      '${files.map((File f) => '  ${_basename(f)}').join('\n')}\n'
      'Adjust the name matching in gp_golden to whatever this Flutter '
      'version ships.',
    );
  }

  await _loadFamily(goldenFontFamily, roboto);
  await _loadFamily('MaterialIcons', icons);
  _loaded = true;
}

/// Where the Flutter SDK keeps the fonts it bundles into applications.
List<Directory> _candidateFontDirectories() {
  final List<String> roots = <String>[];

  // The flutter tool exports this for every process it spawns, tests
  // included. It is the reliable path.
  final String? fromEnvironment = Platform.environment['FLUTTER_ROOT'];
  if (fromEnvironment != null && fromEnvironment.isNotEmpty) {
    roots.add(fromEnvironment.replaceAll(r'\', '/'));
  }

  // Fallback for a test driven by a Dart VM directly: the executable lives
  // at $FLUTTER_ROOT/bin/cache/dart-sdk/bin/dart, four directories down.
  final List<String> parts =
      Platform.resolvedExecutable.replaceAll(r'\', '/').split('/');
  if (parts.length > 5) {
    roots.add(parts.sublist(0, parts.length - 5).join('/'));
  }

  return roots
      .map((String root) =>
          Directory('$root/bin/cache/artifacts/material_fonts'))
      .toList(growable: false);
}

String _basename(File file) => file.path.replaceAll(r'\', '/').split('/').last;

Future<void> _loadFamily(String family, List<File> files) async {
  final FontLoader loader = FontLoader(family);
  for (final File file in files) {
    loader.addFont(
      file.readAsBytes().then((Uint8List bytes) => ByteData.view(bytes.buffer)),
    );
  }
  await loader.load();
}

/// Apply the loaded font family to a theme.
///
/// `ThemeData.copyWith` takes no `fontFamily`, and applying it to the text
/// theme is what actually matters: `gp_ui`'s styles leave the family null, so
/// they inherit whatever the enclosing `DefaultTextStyle` carries. Set it on
/// the theme's text styles and every widget below picks it up through the
/// normal merge.
ThemeData goldenTheme(ThemeData base) => base.copyWith(
      textTheme: base.textTheme.apply(fontFamily: goldenFontFamily),
      primaryTextTheme:
          base.primaryTextTheme.apply(fontFamily: goldenFontFamily),
    );

/// A screen size to render at.
@immutable
class GoldenDevice {
  const GoldenDevice({
    required this.name,
    required this.size,
    this.devicePixelRatio = 2.0,
    this.textScale = 1.0,
  });

  final String name;
  final Size size;
  final double devicePixelRatio;
  final double textScale;

  /// The common mid-range Android the member base actually carries. Narrower
  /// than an iPhone, so it is the size that finds cramped layouts first.
  static const GoldenDevice android =
      GoldenDevice(name: 'android', size: Size(360, 800));

  /// A current iPhone.
  static const GoldenDevice iphone =
      GoldenDevice(name: 'iphone', size: Size(390, 844));

  /// The same phone with the system font scaled up. Not a nicety: a SACCO
  /// member base skews older, large text is the first accessibility setting
  /// anyone turns on, and it is the setting that breaks fixed height rows.
  static const GoldenDevice androidLargeText = GoldenDevice(
    name: 'android-large-text',
    size: Size(360, 800),
    textScale: 1.3,
  );
}

/// Render [child] at [device] and settle it, ready for a golden capture.
///
/// The frame is a plain `MaterialApp` rather than the real router, so a
/// golden shows one screen in one state and nothing about how it was reached.
Future<void> pumpGolden(
  WidgetTester tester,
  Widget child, {
  required ThemeData theme,
  GoldenDevice device = GoldenDevice.android,
}) async {
  await loadGoldenFonts();

  tester.view.physicalSize = Size(
    device.size.width * device.devicePixelRatio,
    device.size.height * device.devicePixelRatio,
  );
  tester.view.devicePixelRatio = device.devicePixelRatio;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
    MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: goldenTheme(theme),
      home: MediaQuery(
        data: MediaQueryData(
          size: device.size,
          devicePixelRatio: device.devicePixelRatio,
          textScaler: TextScaler.linear(device.textScale),
        ),
        child: child,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

/// Capture the whole frame to `test/goldens/<name>.png`.
///
/// The file path is relative to the calling package's test directory, which
/// is where `flutter test` resolves golden paths from.
Future<void> expectGolden(WidgetTester tester, String name) async {
  await expectLater(
    find.byType(MaterialApp),
    matchesGoldenFile('goldens/$name.png'),
  );
}
