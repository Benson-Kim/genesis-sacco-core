/// A countdown to a moment.
///
/// Takes an absolute [deadline] and recomputes the remaining time from the
/// clock on every tick, rather than decrementing a stored number. The
/// difference matters for the same reason it mattered in the inactivity
/// monitor: timers are suspended with the process, so a decrementing counter
/// pauses when the app is backgrounded and comes back claiming more time is
/// left than really is. A member reading "3:20 remaining" against a code that
/// expired while they were in their messages app has been told something
/// false about the only thing this widget exists to say.
library;

import 'dart:async';

import 'package:flutter/widgets.dart';

import '../tokens/typography.dart';

class GpCountdown extends StatefulWidget {
  const GpCountdown({
    required this.deadline,
    super.key,
    this.prefix = 'Expires in',
    this.expired = 'This code has expired.',
    this.style,
    this.now = DateTime.now,
    this.onExpired,
  });

  final DateTime deadline;
  final String prefix;

  /// Shown once the deadline passes. Says the code is dead rather than
  /// showing 0:00, which reads like a display that has got stuck.
  final String expired;

  final TextStyle? style;

  /// Injected so the behaviour is testable without waiting.
  final DateTime Function() now;

  /// Fires once, when the deadline passes while this is on screen. Screens
  /// use it to enable "Resend" at the moment it becomes the useful action.
  final VoidCallback? onExpired;

  @override
  State<GpCountdown> createState() => _GpCountdownState();
}

class _GpCountdownState extends State<GpCountdown> {
  Timer? _ticker;
  bool _fired = false;

  @override
  void initState() {
    super.initState();
    _start();
  }

  @override
  void didUpdateWidget(GpCountdown old) {
    super.didUpdateWidget(old);
    if (old.deadline != widget.deadline) {
      // A resend moves the deadline. Without this the display would keep
      // counting to the old one.
      _fired = false;
      _start();
    }
  }

  void _start() {
    _ticker?.cancel();
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) => _tick());
  }

  void _tick() {
    if (!mounted) {
      return;
    }
    setState(() {});
    if (_remaining <= Duration.zero && !_fired) {
      _fired = true;
      _ticker?.cancel();
      widget.onExpired?.call();
    }
  }

  Duration get _remaining => widget.deadline.difference(widget.now());

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final Duration left = _remaining;
    final TextStyle style = widget.style ?? GpTypography.bodySmall;
    if (left <= Duration.zero) {
      return Text(widget.expired, style: style, textAlign: TextAlign.center);
    }
    final String minutes = left.inMinutes.toString();
    final String seconds =
        (left.inSeconds % 60).toString().padLeft(2, '0');
    return Text(
      '${widget.prefix} $minutes:$seconds',
      textAlign: TextAlign.center,
      // Tabular figures, so the line does not jitter sideways as the digits
      // change. It is on screen for five minutes; a twitching countdown is
      // five minutes of small irritation.
      style: style.copyWith(
        fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
      ),
    );
  }
}
