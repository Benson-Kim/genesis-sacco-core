import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../features/auth/data/auth_notifier.dart';
import '../features/auth/presentation/otp_screen.dart';
import '../features/auth/presentation/phone_screen.dart';
import '../features/dashboard/presentation/dashboard_screen.dart';

/// Route names.
abstract final class Routes {
  static const phone = '/';
  static const otp = '/otp';
  static const dashboard = '/dashboard';
}

final routerProvider = Provider<GoRouter>((ref) {
  final authState = ref.watch(authNotifierProvider);

  return GoRouter(
    initialLocation: Routes.phone,
    redirect: (context, state) {
      final isAuthenticated = authState is AuthAuthenticated;
      final isOnAuth = state.matchedLocation == Routes.phone ||
          state.matchedLocation == Routes.otp;

      if (isAuthenticated && isOnAuth) return Routes.dashboard;
      if (!isAuthenticated && !isOnAuth) return Routes.phone;
      return null;
    },
    routes: [
      GoRoute(
        path: Routes.phone,
        builder: (context, state) => const PhoneScreen(),
      ),
      GoRoute(
        path: Routes.otp,
        builder: (context, state) {
          final phone = state.extra as String? ?? '';
          return OtpScreen(phone: phone);
        },
      ),
      GoRoute(
        path: Routes.dashboard,
        builder: (context, state) => const DashboardScreen(),
      ),
    ],
  );
});
