/// Genesis Prestige member app entry point.
///
/// One white-label build per SACCO: the flavor is chosen here at compile time
/// and injected as a provider override, so nothing downstream can ask for a
/// different tenant.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import 'src/core/env.dart';
import 'src/core/inactivity.dart';
import 'src/core/providers.dart';
import 'src/core/router.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final ProviderContainer container = ProviderContainer(
    overrides: <Override>[flavorProvider.overrideWithValue(Flavor.dev)],
  );

  // Restore before the first frame so the router's first redirect is made
  // against the real session state rather than a default that flashes the
  // sign-in screen at an already-signed-in member.
  await container.read(sessionProvider).restore();

  // Started here rather than inside its provider, because start() registers a
  // WidgetsBindingObserver and the binding exists only from this point on.
  final InactivityMonitor monitor = container.read(inactivityMonitorProvider);
  monitor.start();

  runApp(
    UncontrolledProviderScope(
      container: container,
      child: InactivityScope(
        monitor: monitor,
        child: const MemberApp(),
      ),
    ),
  );
}

class MemberApp extends ConsumerWidget {
  const MemberApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return MaterialApp.router(
      title: 'Genesis Prestige',
      theme: GpTheme.light,
      routerConfig: ref.watch(routerProvider),
      debugShowCheckedModeBanner: false,
    );
  }
}
