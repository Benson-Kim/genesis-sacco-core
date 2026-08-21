/// Three screens before sign in, and a Skip on every one of them.
///
/// The only part of the app that needs no backend whatsoever, which is why it
/// could be built the day it was asked for.
///
/// # No illustrations, deliberately
///
/// The concept sheets carry drawn figures, and they are the reason the mocks
/// read warmly. They are also stock art of people who are not this SACCO's
/// members, and shipping them would put invented faces on a co-operative's
/// front door. Until there are commissioned illustrations or real member
/// photography with consent on file, this uses the brand's own geometry — the
/// mark, the gradient, a single icon — which is honest and costs nothing to
/// replace later. The layout leaves the space; only the content is pending.
///
/// # Skip is on every screen, and it is not a formality
///
/// Onboarding is shown once, and the people most likely to skip it are the
/// ones who have used the app before and reinstalled it. Making them page
/// through three screens to reach a sign in form they already know is a tax
/// on the members who need the least help.
library;

import 'package:flutter/material.dart';
import 'package:gp_ui/gp_ui.dart';

/// One page of the carousel.
@immutable
class OnboardingPage {
  const OnboardingPage({
    required this.icon,
    required this.title,
    required this.body,
  });

  final IconData icon;
  final String title;
  final String body;
}

class OnboardingScreen extends StatefulWidget {
  const OnboardingScreen({required this.onDone, super.key});

  /// Called by Skip and by Get started alike. Onboarding decides nothing; it
  /// only gets out of the way.
  final VoidCallback onDone;

  /// Copy written for a SACCO member rather than for a bank customer: shares
  /// and guarantees are the words this audience actually uses, and they are
  /// what makes a co-operative different from a bank.
  static const List<OnboardingPage> pages = <OnboardingPage>[
    OnboardingPage(
      icon: Icons.savings_rounded,
      title: 'Your SACCO, in your pocket',
      body: 'Check your savings, shares and loans whenever you want to, '
          'without queueing at the office.',
    ),
    OnboardingPage(
      icon: Icons.handshake_rounded,
      title: 'Guarantee with confidence',
      body: 'See what you have been asked to stand behind, and decide in '
          'your own time.',
    ),
    OnboardingPage(
      icon: Icons.lock_rounded,
      title: 'Yours alone',
      body: 'A code to your own phone every time you sign in, and figures '
          'you can hide with one tap.',
    ),
  ];

  @override
  State<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends State<OnboardingScreen> {
  final PageController _pages = PageController();
  int _index = 0;

  @override
  void dispose() {
    _pages.dispose();
    super.dispose();
  }

  bool get _last => _index == OnboardingScreen.pages.length - 1;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: GpPalette.bg,
      body: SafeArea(
        child: Column(
          children: <Widget>[
            Align(
              alignment: Alignment.centerRight,
              child: Padding(
                padding: const EdgeInsets.only(right: GpSpace.sm),
                child: GpTextAction(
                  key: const Key('onboarding.skip'),
                  label: 'Skip',
                  onPressed: widget.onDone,
                ),
              ),
            ),
            Expanded(
              child: PageView.builder(
                controller: _pages,
                itemCount: OnboardingScreen.pages.length,
                onPageChanged: (int i) => setState(() => _index = i),
                itemBuilder: (BuildContext context, int i) =>
                    _Page(page: OnboardingScreen.pages[i]),
              ),
            ),
            _Dots(count: OnboardingScreen.pages.length, index: _index),
            Padding(
              padding: const EdgeInsets.fromLTRB(
                GpSpace.xl,
                GpSpace.xl,
                GpSpace.xl,
                GpSpace.xl,
              ),
              child: GpPrimaryButton(
                key: const Key('onboarding.next'),
                label: _last ? 'Get started' : 'Next',
                icon: _last ? null : Icons.arrow_forward_rounded,
                onPressed: _last
                    ? widget.onDone
                    : () => _pages.nextPage(
                          duration: const Duration(milliseconds: 260),
                          curve: Curves.easeOut,
                        ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Page extends StatelessWidget {
  const _Page({required this.page});

  final OnboardingPage page;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: GpSpace.xl),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            // The space an illustration will occupy. Filled with the brand's
            // own gradient rather than left blank, so the layout reads as
            // finished while the artwork is still pending.
            Container(
              width: 200,
              height: 200,
              decoration: const BoxDecoration(
                shape: BoxShape.circle,
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: <Color>[GpPalette.navy, GpPalette.navyMid],
                ),
              ),
              child: Icon(page.icon, size: 76, color: Colors.white),
            ),
            const SizedBox(height: GpSpace.xxxl),
            Text(
              page.title,
              textAlign: TextAlign.center,
              style: GpTypography.displayLarge.copyWith(fontSize: 24),
            ),
            const SizedBox(height: GpSpace.md),
            Text(
              page.body,
              textAlign: TextAlign.center,
              style: GpTypography.bodyLarge.copyWith(color: GpPalette.sub),
            ),
          ],
        ),
      );
}

class _Dots extends StatelessWidget {
  const _Dots({required this.count, required this.index});

  final int count;
  final int index;

  @override
  Widget build(BuildContext context) => Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: <Widget>[
          for (int i = 0; i < count; i++)
            Container(
              width: i == index ? 22 : 8,
              height: 8,
              margin: const EdgeInsets.symmetric(horizontal: 3),
              decoration: BoxDecoration(
                color: i == index ? GpPalette.navy : GpPalette.line,
                borderRadius: BorderRadius.circular(GpRadius.pill),
              ),
            ),
        ],
      );
}
