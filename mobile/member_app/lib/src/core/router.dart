/// Routing and the session redirect.
///
/// The router is the only place that decides where an unauthenticated app may
/// go. Screens never check the session themselves — a screen that guards
/// itself is a screen someone can forget to guard.
///
/// There are four routes and no fifth. In particular there is no route for
/// "a code has been sent" or "choosing a new PIN": those states belong to
/// their controllers, and giving them addresses would make half-authenticated
/// steps deep-linkable and restorable, which the redirect would then have to
/// be taught to refuse.
library;

import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:gp_ui/gp_ui.dart';

import '../features/auth/presentation/pin_sign_in_screen.dart';
import '../features/auth/presentation/sign_in_screen.dart';
import '../features/onboarding/presentation/onboarding_screen.dart';
import '../features/shell/presentation/app_shell.dart';
import 'env.dart';
import 'providers.dart';
import 'session.dart';

abstract final class Routes {
  static const String splash = '/';
  static const String welcome = '/welcome';
  static const String signIn = '/sign-in';
  static const String home = '/home';
}

/// Whether onboarding has been shown.
///
/// In memory, so it is shown once per launch to a signed-out member and never
/// to a signed-in one. Showing it once EVER needs a durable preference, and
/// the only storage this app has is the secure one holding the refresh token
/// — the wrong place for a display flag, and purged on logout anyway. A small
/// preferences store is the follow-up; until then, Skip is on every page and
/// the cost of the gap is one extra tap for a member who has signed out.
final StateProvider<bool> onboardingSeenProvider =
    StateProvider<bool>((Ref ref) => false);

/// Rebuilds the redirect whenever the session flips, so a 401 anywhere in the
/// app lands the member on sign-in without every caller handling it.
class _SessionListenable extends ChangeNotifier {
  _SessionListenable(MemberSession session) {
    _subscription = session.states.listen((_) => notifyListeners());
  }

  late final StreamSubscription<SessionState> _subscription;

  @override
  void dispose() {
    _subscription.cancel();
    super.dispose();
  }
}

final Provider<GoRouter> routerProvider = Provider<GoRouter>((Ref ref) {
  final MemberSession session = ref.watch(sessionProvider);
  final AuthMode mode = ref.watch(flavorProvider).authMode;
  final _SessionListenable listenable = _SessionListenable(session);
  ref.onDispose(listenable.dispose);

  return GoRouter(
    initialLocation: Routes.splash,
    refreshListenable: listenable,
    redirect: (BuildContext context, GoRouterState state) {
      final bool signedIn = session.state == SessionState.signedIn;
      final String at = state.matchedLocation;
      if (signedIn) {
        return at == Routes.home ? null : Routes.home;
      }
      if (!ref.read(onboardingSeenProvider)) {
        return at == Routes.welcome ? null : Routes.welcome;
      }
      return at == Routes.signIn ? null : Routes.signIn;
    },
    routes: <RouteBase>[
      GoRoute(
        path: Routes.splash,
        builder: (BuildContext context, GoRouterState state) =>
            const GpLoadingView(),
      ),
      GoRoute(
        path: Routes.welcome,
        builder: (BuildContext context, GoRouterState state) =>
            OnboardingScreen(
          onDone: () {
            ref.read(onboardingSeenProvider.notifier).state = true;
            context.go(Routes.signIn);
          },
        ),
      ),
      GoRoute(
        path: Routes.signIn,
        // The flavor picks the sign in, and it picks at build time. An app
        // that could switch its own authentication at runtime would be an app
        // whose authentication a server response can change.
        builder: (BuildContext context, GoRouterState state) =>
            switch (mode) {
          AuthMode.otpOnly => const SignInScreen(),
          AuthMode.pinThenOtp => const PinSignInScreen(),
        },
      ),
      GoRoute(
        path: Routes.home,
        // One route for the whole signed-in app: the five tabs are shell
        // state, not addresses. Giving each tab a route would make a locked
        // one reachable by URL, and the lock would then have to be enforced
        // in the router as well as in the bar and the shell.
        builder: (BuildContext context, GoRouterState state) =>
            const AppShell(),
      ),
    ],
  );
});
