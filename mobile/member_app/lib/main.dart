import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';
import 'src/core/offline_cache.dart';
import 'src/core/router.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await OfflineCache.init();
  runApp(const ProviderScope(child: MemberApp()));
}

class MemberApp extends ConsumerWidget {
  const MemberApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final router = ref.watch(routerProvider);
    return MaterialApp.router(
      title: 'Genesis Prestige',
      theme: GpTheme.light,
      routerConfig: router,
      debugShowCheckedModeBanner: false,
    );
  }
}
