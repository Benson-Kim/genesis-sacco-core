/// Inactivity logout guards (#43 T0).
///
/// The test that matters most is `_a suspended process_`: it drives the
/// lifecycle callback directly with NO timer having fired, which is exactly
/// what a locked phone does. A timer-only implementation passes every other
/// test in this file and fails that one.
library;

import 'dart:async';

import 'package:fake_async/fake_async.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:member_app/src/core/inactivity.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const Duration timeout = Duration(minutes: 5);
  final DateTime start = DateTime.utc(2026, 8, 21, 9);

  group('isExpired', () {
    test('is false before the deadline', () {
      expect(
        isExpired(
          lastActivity: start,
          now: start.add(const Duration(minutes: 4, seconds: 59)),
          timeout: timeout,
        ),
        isFalse,
      );
    });

    test('is true exactly at the deadline', () {
      expect(
        isExpired(
          lastActivity: start,
          now: start.add(timeout),
          timeout: timeout,
        ),
        isTrue,
      );
    });

    test('a clock that went backwards does not expire the session', () {
      // Device clocks move: a timezone change, an NTP correction, a user
      // setting the date. A negative elapsed time is nonsense, and the safe
      // reading of nonsense here is "no evidence of inactivity" — the next
      // real interaction re-anchors the deadline.
      expect(
        isExpired(
          lastActivity: start,
          now: start.subtract(const Duration(hours: 2)),
          timeout: timeout,
        ),
        isFalse,
      );
    });
  });

  group('a suspended process — the case a timer cannot cover', () {
    test('resuming after the timeout ends the session, with no timer having '
        'fired', () async {
      DateTime now = start;
      int expiries = 0;
      final InactivityMonitor monitor = InactivityMonitor(
        timeout: timeout,
        onExpired: () async => expiries++,
        now: () => now,
      );
      addTearDown(monitor.dispose);
      monitor.start(observeLifecycle: false);

      // The phone is locked and pocketed. No timer runs; the process is not
      // scheduled at all. Only the wall clock moves.
      monitor.didChangeAppLifecycleState(AppLifecycleState.paused);
      now = start.add(const Duration(hours: 2));
      monitor.didChangeAppLifecycleState(AppLifecycleState.resumed);
      await Future<void>.delayed(Duration.zero);

      expect(expiries, 1,
          reason: 'a timer-only monitor finds its timer still pending here '
              'and carries on with a two-hour-old session');
    });

    test('resuming inside the timeout keeps the session', () async {
      DateTime now = start;
      int expiries = 0;
      final InactivityMonitor monitor = InactivityMonitor(
        timeout: timeout,
        onExpired: () async => expiries++,
        now: () => now,
      );
      addTearDown(monitor.dispose);
      monitor.start(observeLifecycle: false);

      monitor.didChangeAppLifecycleState(AppLifecycleState.paused);
      now = start.add(const Duration(minutes: 1));
      monitor.didChangeAppLifecycleState(AppLifecycleState.resumed);
      await Future<void>.delayed(Duration.zero);

      expect(expiries, 0);
    });
  });

  group('the foreground timer', () {
    test('ends the session once the timeout elapses untouched', () {
      fakeAsync((FakeAsync async) {
        DateTime now = start;
        int expiries = 0;
        final InactivityMonitor monitor = InactivityMonitor(
          timeout: timeout,
          onExpired: () async => expiries++,
          now: () => now,
        );
        monitor.start(observeLifecycle: false);

        now = start.add(timeout);
        async.elapse(timeout);
        async.flushMicrotasks();

        expect(expiries, 1);
        monitor.dispose();
      });
    });

    test('an interaction moves the deadline', () {
      fakeAsync((FakeAsync async) {
        DateTime now = start;
        int expiries = 0;
        final InactivityMonitor monitor = InactivityMonitor(
          timeout: timeout,
          onExpired: () async => expiries++,
          now: () => now,
        );
        monitor.start(observeLifecycle: false);

        // Four minutes in, the member taps something.
        now = start.add(const Duration(minutes: 4));
        async.elapse(const Duration(minutes: 4));
        monitor.poke();
        expect(monitor.lastActivity, now);

        // One more minute takes the ORIGINAL deadline past, but not the new
        // one. Remove the reschedule in poke() and this fails.
        now = now.add(const Duration(minutes: 1));
        async.elapse(const Duration(minutes: 1));
        async.flushMicrotasks();
        expect(expiries, 0);

        // Four more minutes reaches the new deadline.
        now = now.add(timeout);
        async.elapse(timeout);
        async.flushMicrotasks();
        expect(expiries, 1);

        monitor.dispose();
      });
    });

    test('time already served is not refunded by an interim check', () {
      fakeAsync((FakeAsync async) {
        DateTime now = start;
        int expiries = 0;
        final InactivityMonitor monitor = InactivityMonitor(
          timeout: timeout,
          onExpired: () async => expiries++,
          now: () => now,
        );
        monitor.start(observeLifecycle: false);

        // A check just short of the deadline must reschedule for the REMAINING
        // time, not for another full window — otherwise repeated near-misses
        // walk the session past its timeout indefinitely.
        now = start.add(const Duration(minutes: 4, seconds: 59));
        async.elapse(const Duration(minutes: 4, seconds: 59));
        unawaited(monitor.evaluate());
        async.flushMicrotasks();
        expect(expiries, 0);

        now = now.add(const Duration(seconds: 1));
        async.elapse(const Duration(seconds: 1));
        async.flushMicrotasks();
        expect(expiries, 1,
            reason: 'the reschedule must cover only the second that was left');

        monitor.dispose();
      });
    });
  });

  group('expiring is a one-shot', () {
    test('a second expiry cannot start while the first is being handled',
        () async {
      DateTime now = start;
      int expiries = 0;
      final InactivityMonitor monitor = InactivityMonitor(
        timeout: timeout,
        onExpired: () async => expiries++,
        now: () => now,
      );
      addTearDown(monitor.dispose);
      monitor.start(observeLifecycle: false);

      now = start.add(const Duration(hours: 1));
      await monitor.evaluate();
      await monitor.evaluate();
      monitor.poke();
      await monitor.evaluate();

      expect(expiries, 1,
          reason: 'session teardown must not be started twice, and a poke '
              'after expiry must not revive a dead session');
    });
  });
}
