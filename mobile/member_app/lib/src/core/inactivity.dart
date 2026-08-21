/// Inactivity logout (#43 T0).
///
/// The session ends after [InactivityMonitor.timeout] of no interaction,
/// regardless of how long the tokens themselves remain valid. A 15-minute
/// access token says nothing about whether the person holding the phone is
/// still the member: the device on the matatu seat is signed in until someone
/// decides it is not, and that decision is this file.
///
/// # Why a timer alone is not enough
///
/// The obvious implementation is a `Timer` that fires after the timeout. It is
/// also the one that fails in the only case that matters. Timers are suspended
/// with the process: a phone locked and pocketed for an hour may run no timer
/// at all, and on resume the app finds its timer still pending and cheerfully
/// carries on with a session that has been unattended since breakfast.
///
/// So expiry is decided by WALL CLOCK, and the timer is only a prompt to look.
/// [isExpired] compares timestamps and is the whole rule; the timer covers the
/// foreground case, and the lifecycle observer covers the case where no timer
/// ever ran.
library;

import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:meta/meta.dart';

/// Whether a session last touched at [lastActivity] has gone stale by [now].
///
/// A free function, so the rule can be tested without a binding, a timer, or a
/// widget tree — the parts most likely to be got wrong are then the parts most
/// cheaply checked.
@visibleForTesting
bool isExpired({
  required DateTime lastActivity,
  required DateTime now,
  required Duration timeout,
}) =>
    !now.difference(lastActivity).isNegative &&
    now.difference(lastActivity) >= timeout;

/// Watches for inactivity and ends the session once when it finds it.
class InactivityMonitor with WidgetsBindingObserver {
  InactivityMonitor({
    required this.timeout,
    required Future<void> Function() onExpired,
    DateTime Function() now = DateTime.now,
  })  : _onExpired = onExpired,
        _now = now;

  final Duration timeout;
  final Future<void> Function() _onExpired;
  final DateTime Function() _now;

  DateTime? _lastActivity;
  Timer? _timer;
  bool _fired = false;

  @visibleForTesting
  DateTime? get lastActivity => _lastActivity;

  /// Begin watching. Registers the lifecycle observer, so the resume path
  /// works even when the process was suspended before any timer could fire.
  void start({bool observeLifecycle = true}) {
    if (observeLifecycle) {
      WidgetsBinding.instance.addObserver(this);
    }
    _fired = false;
    poke();
  }

  /// Record an interaction. Cheap on purpose: it runs on every pointer event
  /// in the app, so it does no more than move a timestamp and reset a timer.
  void poke() {
    if (_fired) {
      return;
    }
    _lastActivity = _now();
    _restart();
  }

  /// Check the clock and end the session if it has run out.
  ///
  /// Idempotent: [_fired] means a monitor that expires while a teardown is
  /// already in flight cannot start a second one.
  Future<void> evaluate() async {
    final DateTime? last = _lastActivity;
    if (_fired || last == null) {
      return;
    }
    if (!isExpired(lastActivity: last, now: _now(), timeout: timeout)) {
      _restart();
      return;
    }
    _fired = true;
    _timer?.cancel();
    _timer = null;
    await _onExpired();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      // The important arm. The elapsed time is read from the clock, not from
      // how much of the timer happened to run while the process was alive.
      unawaited(evaluate());
      return;
    }
    // Backgrounded: a pending timer buys nothing, because resume re-checks the
    // clock anyway. Cancel it rather than hold a wakeup we do not need.
    _timer?.cancel();
    _timer = null;
  }

  void _restart() {
    _timer?.cancel();
    final DateTime? last = _lastActivity;
    if (last == null) {
      return;
    }
    // Time already served counts. Without this an `evaluate()` a moment before
    // the deadline would restart the full window and the session would drift
    // past its timeout, one near-miss at a time.
    final Duration remaining = timeout - _now().difference(last);
    _timer = Timer(
      remaining.isNegative ? Duration.zero : remaining,
      () => unawaited(evaluate()),
    );
  }

  void dispose() {
    _timer?.cancel();
    _timer = null;
    WidgetsBinding.instance.removeObserver(this);
  }
}

/// Reports every pointer interaction beneath it to [monitor].
///
/// A [Listener] rather than a [GestureDetector]: it sees pointer events during
/// the hit-test walk, before any child claims the gesture, so scrolling a list
/// or typing into a field counts as activity. A GestureDetector here would see
/// only the taps nothing else wanted.
class InactivityScope extends StatelessWidget {
  const InactivityScope({
    required this.monitor,
    required this.child,
    super.key,
  });

  final InactivityMonitor monitor;
  final Widget child;

  @override
  Widget build(BuildContext context) => Listener(
        behavior: HitTestBehavior.translucent,
        onPointerDown: (PointerDownEvent _) => monitor.poke(),
        onPointerSignal: (PointerSignalEvent _) => monitor.poke(),
        child: child,
      );
}
