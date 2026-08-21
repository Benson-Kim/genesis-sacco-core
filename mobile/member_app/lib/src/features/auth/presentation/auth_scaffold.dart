/// The chrome every auth screen shares: the gradient field, the floating
/// card, the brand lockup, an optional back arrow, and the footnote.
///
/// Extracted the moment there was a second auth screen. The sign in gate, the
/// PIN entry, the code step, the reset flow and the first-PIN screen are five
/// screens with one frame, and five copies of a frame is five places for it
/// to drift.
///
/// The back arrow sits ON the gradient rather than inside the card, so the
/// card stays a single object holding one question. That also keeps it clear
/// of the keyboard, which covers the bottom two thirds of the screen for most
/// of this flow.
library;

import 'package:flutter/material.dart';
import 'package:gp_ui/gp_ui.dart';

class AuthScaffold extends StatelessWidget {
  const AuthScaffold({
    required this.title,
    required this.subtitle,
    required this.children,
    super.key,
    this.onBack,
    this.footnote = 'Protected by a one-time code · Kenya DPA 2019',
  });

  final String title;

  /// A widget rather than a string: several of these screens need part of
  /// the sentence emphasised, and a screen that has to pass rich text through
  /// a `String` ends up formatting it somewhere worse.
  final Widget subtitle;

  final List<Widget> children;

  /// Null hides the arrow. A screen with nowhere to go back to should not
  /// show a control that does nothing.
  final VoidCallback? onBack;

  final String footnote;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: <Color>[GpPalette.navy, GpPalette.navyMid],
          ),
        ),
        child: SafeArea(
          child: Column(
            children: <Widget>[
              SizedBox(
                height: GpSpace.touchTarget,
                child: onBack == null
                    ? null
                    : Align(
                        alignment: Alignment.centerLeft,
                        child: IconButton(
                          key: const Key('auth.back'),
                          onPressed: onBack,
                          tooltip: 'Back',
                          icon: const Icon(
                            Icons.arrow_back_rounded,
                            color: Colors.white,
                          ),
                        ),
                      ),
              ),
              Expanded(
                child: Center(
                  child: SingleChildScrollView(
                    padding: const EdgeInsets.fromLTRB(
                      GpSpace.xl,
                      0,
                      GpSpace.xl,
                      GpSpace.xl,
                    ),
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 440),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: <Widget>[
                          _GateCard(
                            title: title,
                            subtitle: subtitle,
                            children: children,
                          ),
                          const SizedBox(height: GpSpace.xl),
                          Text(
                            footnote,
                            textAlign: TextAlign.center,
                            style: GpTypography.bodySmall.copyWith(
                              color: Colors.white.withValues(alpha: .72),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The prototype's `.gatecard`: white, 20px corners, and the one real shadow
/// in this design system.
class _GateCard extends StatelessWidget {
  const _GateCard({
    required this.title,
    required this.subtitle,
    required this.children,
  });

  final String title;
  final Widget subtitle;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(GpSpace.xxl),
        decoration: BoxDecoration(
          color: GpPalette.card,
          borderRadius: BorderRadius.circular(GpRadius.hero),
          boxShadow: <BoxShadow>[
            BoxShadow(
              color: GpPalette.ink.withValues(alpha: .28),
              blurRadius: 48,
              offset: const Offset(0, 18),
            ),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            const GpBrandLockup(eyebrow: 'Zuri Genesis · SACCO'),
            const SizedBox(height: GpSpace.xl),
            Text(title, style: GpTypography.headlineMedium),
            const SizedBox(height: GpSpace.xs),
            subtitle,
            const SizedBox(height: GpSpace.xl),
            ...children,
          ],
        ),
      );
}

/// The message line under a form. Renders what a controller wrote, never a
/// server category.
class AuthMessage extends StatelessWidget {
  const AuthMessage({
    required this.message,
    required this.correlationId,
    super.key,
  });

  final String? message;
  final String? correlationId;

  @override
  Widget build(BuildContext context) {
    final String? text = message;
    if (text == null) {
      return const SizedBox.shrink();
    }
    return Padding(
      padding: const EdgeInsets.only(top: GpSpace.lg),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          GpBanner(
            text,
            key: const Key('auth.message'),
            tone: GpBannerTone.danger,
            icon: Icons.error_outline_rounded,
          ),
          if (correlationId != null) ...<Widget>[
            const SizedBox(height: GpSpace.sm),
            Text('Ref: $correlationId', style: GpTypography.bodySmall),
          ],
        ],
      ),
    );
  }
}
