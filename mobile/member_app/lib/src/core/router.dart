/// Routing and the session redirect.
///
/// The router is the only place that decides where an unauthenticated app may
/// go. Screens never check the session themselves — a screen that guards
/// itself is a screen someone can forget to guard.
library;

import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import 'providers.dart';
import 'session.dart';

abstract final class Routes {
  static const String splash = '/';
  static const String signIn = '/sign-in';
  static const String home = '/home';
}

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
  final _SessionListenable listenable = _SessionListenable(session);
  ref.onDispose(listenable.dispose);

  return GoRouter(
    initialLocation: Routes.splash,
    refreshListenable: listenable,
    redirect: (BuildContext context, GoRouterState state) {
      final bool signedIn = session.state == SessionState.signedIn;
      final bool atSignIn = state.matchedLocation == Routes.signIn;
      if (!signedIn) {
        return atSignIn ? null : Routes.signIn;
      }
      return atSignIn || state.matchedLocation == Routes.splash ? Routes.home : null;
    },
    routes: <RouteBase>[
      GoRoute(
        path: Routes.splash,
        builder: (BuildContext context, GoRouterState state) => const _Placeholder('Starting'),
      ),
      // MR-1 replaces these two with the real OTP flow and the session shell.
      GoRoute(
        path: Routes.signIn,
        builder: (BuildContext context, GoRouterState state) => const _Placeholder('Sign in'),
      ),
      GoRoute(
        path: Routes.home,
        builder: (BuildContext context, GoRouterState state) => const _Placeholder('Home'),
      ),
    ],
  );
});

class _Placeholder extends StatelessWidget {
  const _Placeholder(this.label);

  final String label;

  @override
  Widget build(BuildContext context) => Center(child: Text(label));
}
