/// A tab whose feature has no merged endpoint behind it.
///
/// Reachable only if something routes to it despite the lock, which nothing
/// currently does — the bar refuses to fire and the shell refuses to switch.
/// It exists because "unreachable" and "does not exist" are different states,
/// and the first one should still render something honest if it is ever hit.
///
/// The copy is written for a member, not for us. No endpoint names, no work
/// item numbers, no apology, and no date: a date the backend has not
/// committed to is a promise the app has no business making.
library;

import 'package:flutter/material.dart';
import 'package:gp_ui/gp_ui.dart';

class NotYetTab extends StatelessWidget {
  const NotYetTab({
    required this.title,
    required this.headline,
    required this.message,
    required this.icon,
    super.key,
  });

  final String title;
  final String headline;
  final String message;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(
            GpSpace.gutter,
            GpSpace.lg,
            GpSpace.gutter,
            0,
          ),
          child: Text(title, style: GpTypography.displayLarge),
        ),
        Expanded(
          child: Center(
            child: GpNotYetState(
              title: headline,
              message: message,
              icon: icon,
            ),
          ),
        ),
      ],
    );
  }
}
