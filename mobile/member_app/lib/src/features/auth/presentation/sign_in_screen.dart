/// Sign in: one route, two panes.
///
/// The identifier pane and the code pane share a route on purpose. A separate
/// `/verify-code` route would be a named, reachable location representing a
/// half-authenticated state — deep-linkable, restorable, and something the
/// router would then have to be taught to refuse. The state that says a code
/// is outstanding lives in the controller, is not addressable, and disappears
/// with the flow.
///
/// This layer imports `domain/` only. It never sees an [ApiError], a status
/// code, or a token; the guard sweep in `mobile:analyze` fails the build if a
/// `presentation/` file imports `gp_api_client` at all.
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
      backgroundColor: GpPalette.bg,
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: switch (state.step) {
                SignInStep.identifier => const _IdentifierPane(),
                SignInStep.code => const _CodePane(),
                // The router redirects on the session flip; this is what the
                // frame between adoption and redirect shows.
                SignInStep.done => const GpLoadingView(),
              },
            ),
          ),
        ),
      ),
    );
  }
}

/// Shared chrome: the wordmark, and the message line beneath the form.
class _Frame extends StatelessWidget {
  const _Frame({
    required this.title,
    required this.subtitle,
    required this.children,
  });

  final String title;
  final String subtitle;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          const Text('Genesis Prestige',
              style: GpTypography.displayLarge, textAlign: TextAlign.center),
          const SizedBox(height: 8),
          Text(title, style: GpTypography.titleMedium),
          const SizedBox(height: 4),
          Text(subtitle, style: GpTypography.bodyMedium),
          const SizedBox(height: 24),
          ...children,
        ],
      );
}

/// The message line.
///
/// Renders [SignInState.message], which the controller wrote, and the
/// correlation id when there is one. It cannot render a server category
/// because it is never given one.
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
      padding: const EdgeInsets.only(top: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(text,
              key: const Key('signIn.message'),
              style: GpTypography.bodyMedium.copyWith(color: GpPalette.brick)),
          if (correlationId != null) ...<Widget>[
            const SizedBox(height: 4),
            Text('Ref: $correlationId', style: GpTypography.labelSmall),
          ],
        ],
      ),
    );
  }
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
    return _Frame(
      title: 'Sign in',
      subtitle: 'We will send a six-digit code to confirm it is you.',
      children: <Widget>[
        TextField(
          key: const Key('signIn.identifier'),
          controller: _field,
          enabled: !state.busy,
          autocorrect: false,
          // The field takes a number OR an email, so neither a pure number pad
          // nor a pure email keyboard is right. This one carries the digits,
          // the plus and the at-sign together.
          keyboardType: TextInputType.emailAddress,
          textInputAction: TextInputAction.go,
          decoration: const InputDecoration(
            labelText: 'Mobile number or email',
            hintText: '07XX XXX XXX',
          ),
          onSubmitted: state.busy ? null : _submit,
        ),
        const SizedBox(height: 16),
        ElevatedButton(
          key: const Key('signIn.submit'),
          // Disabled while busy: the first half of FM-G. The second half is
          // the Idempotency-Key, which makes a double submit that slips
          // through harmless rather than merely unlikely.
          onPressed: state.busy ? null : () => _submit(_field.text),
          child: Text(state.busy ? 'Sending…' : 'Send code'),
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
  final TextEditingController _field = TextEditingController();

  @override
  void dispose() {
    _field.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final SignInState state = ref.watch(signInControllerProvider);
    final SignInController controller =
        ref.read(signInControllerProvider.notifier);
    return _Frame(
      title: 'Enter your code',
      // Says where the code went WITHOUT confirming that anything was
      // delivered, because the server does not tell us and must not: an
      // unregistered identifier and a registered one produce byte-identical
      // responses (gate 1.6).
      subtitle: 'If ${state.identifier?.value ?? 'that account'} is '
          'registered, a six-digit code is on its way.',
      children: <Widget>[
        TextField(
          key: const Key('signIn.code'),
          controller: _field,
          enabled: !state.busy,
          autocorrect: false,
          keyboardType: TextInputType.number,
          textInputAction: TextInputAction.go,
          maxLength: 6,
          decoration: const InputDecoration(
            labelText: 'Six-digit code',
            counterText: '',
          ),
          onSubmitted:
              state.busy ? null : (String v) => controller.submitCode(v),
        ),
        const SizedBox(height: 16),
        ElevatedButton(
          key: const Key('signIn.verify'),
          onPressed:
              state.busy ? null : () => controller.submitCode(_field.text),
          child: Text(state.busy ? 'Checking…' : 'Confirm'),
        ),
        const SizedBox(height: 8),
        TextButton(
          key: const Key('signIn.resend'),
          onPressed: state.busy ? null : controller.resend,
          child: const Text('Send a new code'),
        ),
        TextButton(
          key: const Key('signIn.restart'),
          onPressed: state.busy ? null : controller.restart,
          child: const Text('Use a different number or email'),
        ),
        _Message(message: state.message, correlationId: state.correlationId),
      ],
    );
  }
}
