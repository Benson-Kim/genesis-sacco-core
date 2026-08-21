/// Sign in: the prototype's gate, on a phone.
///
/// The desktop console puts sign in behind a full screen navy gradient with a
/// single white card floating on it, and that treatment survives the move to
/// mobile better than most of the prototype does — it is the one screen with
/// no navigation, no data and one job, so a card on a coloured ground is
/// exactly right at any size.
///
/// The identifier pane and the code pane share a route on purpose. A separate
/// `/verify-code` route would be a named, reachable location representing a
/// half-authenticated state — deep-linkable, restorable, and something the
/// router would then have to be taught to refuse. The state that says a code
/// is outstanding lives in the controller, is not addressable, and disappears
/// with the flow.
///
/// This layer imports `domain/` and `gp_ui` only. It never sees an
/// `ApiError`, a status code or a token; two guard sweeps in `mobile:analyze`
/// fail the build if that changes.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../domain/sign_in_controller.dart';

class SignInScreen extends ConsumerWidget {
  const SignInScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final SignInState state = ref.watch(signInControllerProvider);
    return Scaffold(
      // The gate's own ground, straight from the prototype: navy to navyMid
      // on the diagonal, edge to edge.
      body: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: <Color>[GpPalette.navy, GpPalette.navyMid],
          ),
        ),
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(GpSpace.xl),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 440),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: <Widget>[
                    _GateCard(
                      child: switch (state.step) {
                        SignInStep.identifier => const _IdentifierPane(),
                        SignInStep.code => const _CodePane(),
                        // The router redirects on the session flip; this is
                        // the frame between adoption and that redirect.
                        SignInStep.done => const Padding(
                            padding: EdgeInsets.symmetric(
                              vertical: GpSpace.xxxl,
                            ),
                            child: GpLoadingView(),
                          ),
                      },
                    ),
                    const SizedBox(height: GpSpace.xl),
                    const _Footnote(),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// The prototype's `.gatecard`: white, 20px corners, and the one real shadow
/// in this design system.
class _GateCard extends StatelessWidget {
  const _GateCard({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(GpSpace.xxl),
        decoration: BoxDecoration(
          color: GpPalette.card,
          borderRadius: BorderRadius.circular(GpRadius.hero),
          boxShadow: <BoxShadow>[
            BoxShadow(
              color: GpPalette.ink.withValues(alpha: 0.28),
              blurRadius: 48,
              offset: const Offset(0, 18),
            ),
          ],
        ),
        child: child,
      );
}

class _Footnote extends StatelessWidget {
  const _Footnote();

  @override
  Widget build(BuildContext context) => Text(
        'Protected by a one-time code · Kenya DPA 2019',
        textAlign: TextAlign.center,
        style: GpTypography.bodySmall.copyWith(
          color: Colors.white.withValues(alpha: 0.72),
        ),
      );
}

/// The message line. Renders what the controller wrote, and the correlation
/// id when there is one. It cannot render a server category because it is
/// never given one.
class _Message extends StatelessWidget {
  const _Message({required this.message, required this.correlationId});

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
            key: const Key('signIn.message'),
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

class _Heading extends StatelessWidget {
  const _Heading({required this.title, required this.subtitle});

  final String title;
  final Widget subtitle;

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const GpBrandLockup(eyebrow: 'Zuri Genesis · SACCO'),
          const SizedBox(height: GpSpace.xl),
          Text(title, style: GpTypography.headlineMedium),
          const SizedBox(height: GpSpace.xs),
          subtitle,
          const SizedBox(height: GpSpace.xl),
        ],
      );
}

class _IdentifierPane extends ConsumerStatefulWidget {
  const _IdentifierPane();

  @override
  ConsumerState<_IdentifierPane> createState() => _IdentifierPaneState();
}

class _IdentifierPaneState extends ConsumerState<_IdentifierPane> {
  final TextEditingController _field = TextEditingController();

  @override
  void dispose() {
    _field.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final SignInState state = ref.watch(signInControllerProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const _Heading(
          title: 'Sign in',
          subtitle: Text(
            'We will send a six-digit code to confirm it is you.',
            style: GpTypography.bodyMedium,
          ),
        ),
        GpField(
          key: const Key('signIn.identifier'),
          label: 'Mobile number or email',
          controller: _field,
          hint: '07XX XXX XXX',
          enabled: !state.busy,
          // The field takes a number OR an email, so neither a number pad nor
          // an email keyboard is right on its own. This one carries the
          // digits, the plus and the at sign together.
          keyboardType: TextInputType.emailAddress,
          textInputAction: TextInputAction.go,
          autofillHints: const <String>[AutofillHints.username],
          onSubmitted: state.busy ? null : _submit,
        ),
        const SizedBox(height: GpSpace.xl),
        GpPrimaryButton(
          key: const Key('signIn.submit'),
          label: 'Send code',
          busyLabel: 'Sending…',
          busy: state.busy,
          // Disabled in flight: the first half of FM-G. The second half is
          // the Idempotency-Key, which makes a double submit that slips
          // through harmless rather than merely unlikely.
          onPressed: () => _submit(_field.text),
        ),
        _Message(message: state.message, correlationId: state.correlationId),
      ],
    );
  }

  void _submit(String value) =>
      ref.read(signInControllerProvider.notifier).submitIdentifier(value);
}

class _CodePane extends ConsumerStatefulWidget {
  const _CodePane();

  @override
  ConsumerState<_CodePane> createState() => _CodePaneState();
}

class _CodePaneState extends ConsumerState<_CodePane> {
  String _code = '';

  @override
  Widget build(BuildContext context) {
    final SignInState state = ref.watch(signInControllerProvider);
    final SignInController controller =
        ref.read(signInControllerProvider.notifier);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        _Heading(
          title: 'Enter your code',
          // Says where the code went WITHOUT confirming that anything was
          // delivered, because the server does not tell us and must not: an
          // unregistered identifier and a registered one produce
          // byte-identical responses (gate 1.6).
          subtitle: Text.rich(
            TextSpan(
              style: GpTypography.bodyMedium,
              children: <InlineSpan>[
                const TextSpan(text: 'If '),
                TextSpan(
                  text: state.identifier?.value ?? 'that account',
                  style: GpTypography.bodyMedium.copyWith(
                    color: GpPalette.ink,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const TextSpan(
                  text: ' is registered, a six-digit code is on its way.',
                ),
              ],
            ),
          ),
        ),
        GpOtpField(
          key: const Key('signIn.code'),
          enabled: !state.busy,
          hasError: state.message != null,
          onChanged: (String value) => _code = value,
          // Submitting on the sixth digit saves a reach for a button the
          // keyboard is probably covering.
          onCompleted: state.busy ? null : controller.submitCode,
        ),
        const SizedBox(height: GpSpace.xl),
        GpPrimaryButton(
          key: const Key('signIn.verify'),
          label: 'Verify and sign in',
          busyLabel: 'Checking…',
          busy: state.busy,
          onPressed: () => controller.submitCode(_code),
        ),
        _Message(message: state.message, correlationId: state.correlationId),
        const SizedBox(height: GpSpace.sm),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            GpTextAction(
              key: const Key('signIn.resend'),
              label: 'Resend',
              emphasis: true,
              onPressed: state.busy ? null : controller.resend,
            ),
            const Text('·', style: GpTypography.bodySmall),
            GpTextAction(
              key: const Key('signIn.restart'),
              label: 'Use another number',
              onPressed: state.busy ? null : controller.restart,
            ),
          ],
        ),
      ],
    );
  }
}
