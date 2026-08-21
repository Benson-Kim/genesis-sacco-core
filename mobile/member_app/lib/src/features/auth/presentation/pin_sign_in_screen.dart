/// Sign in with a member number and a PIN, then a code.
///
/// # The code step says where the code went, and that is new
///
/// The OTP-only screen has to hedge — "if that account is registered, a code
/// is on its way" — because nothing has been proven when the code is sent, so
/// naming a destination would confirm the identifier is real.
///
/// Here the PIN has already been verified before a code is dispatched. The
/// member has proven who they are, so telling them the code went to
/// `07XX XXX 678` reveals nothing they did not already know, and it answers
/// the question they actually have: which of my numbers should I be looking
/// at. The masking is done by the SERVER — a client that masked a full number
/// would be a client that had been sent one.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../../../core/providers.dart';
import '../domain/credential_port.dart';
import '../domain/pin_sign_in_controller.dart';
import 'auth_scaffold.dart';
import 'pin_reset_screen.dart';

class PinSignInScreen extends ConsumerWidget {
  const PinSignInScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final PinSignInStep step = ref.watch(pinSignInControllerProvider).step;
    return switch (step) {
      PinSignInStep.credentials => const _CredentialsPane(),
      PinSignInStep.code => const _CodePane(),
      PinSignInStep.done => const AuthScaffold(
          title: 'Signing you in',
          subtitle: SizedBox.shrink(),
          children: <Widget>[GpLoadingView()],
        ),
    };
  }
}

class _CredentialsPane extends ConsumerStatefulWidget {
  const _CredentialsPane();

  @override
  ConsumerState<_CredentialsPane> createState() => _CredentialsPaneState();
}

class _CredentialsPaneState extends ConsumerState<_CredentialsPane> {
  final TextEditingController _number = TextEditingController();
  final TextEditingController _pin = TextEditingController();
  bool _pinVisible = false;

  @override
  void dispose() {
    _number.dispose();
    _pin.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final PinSignInState state = ref.watch(pinSignInControllerProvider);
    final int length = ref.watch(flavorProvider).pinLength;

    return AuthScaffold(
      title: 'Welcome back',
      subtitle: const Text(
        'Sign in to your account.',
        style: GpTypography.bodyMedium,
      ),
      children: <Widget>[
        GpField(
          key: const Key('pinSignIn.number'),
          label: 'Member number',
          controller: _number,
          hint: 'As on your passbook',
          enabled: !state.busy,
          keyboardType: TextInputType.text,
          textInputAction: TextInputAction.next,
          autofillHints: const <String>[AutofillHints.username],
        ),
        const SizedBox(height: GpSpace.lg),
        GpField(
          key: const Key('pinSignIn.pin'),
          label: 'PIN',
          controller: _pin,
          hint: 'Enter your $length digit PIN',
          enabled: !state.busy,
          obscure: !_pinVisible,
          keyboardType: TextInputType.number,
          textInputAction: TextInputAction.go,
          autofillHints: const <String>[AutofillHints.password],
          inputFormatters: <TextInputFormatter>[
            FilteringTextInputFormatter.digitsOnly,
            LengthLimitingTextInputFormatter(length),
          ],
          // A reveal control, because a PIN typed wrong twice on a small
          // keyboard is how people get locked out of their own money. It
          // defaults to hidden and the member chooses.
          trailing: IconButton(
            key: const Key('pinSignIn.reveal'),
            onPressed: () => setState(() => _pinVisible = !_pinVisible),
            tooltip: _pinVisible ? 'Hide PIN' : 'Show PIN',
            icon: Icon(
              _pinVisible
                  ? Icons.visibility_off_rounded
                  : Icons.visibility_rounded,
              size: 20,
              color: GpPalette.sub,
            ),
          ),
          onSubmitted: state.busy ? null : (_) => _submit(),
        ),
        Align(
          alignment: Alignment.centerLeft,
          child: GpTextAction(
            key: const Key('pinSignIn.forgot'),
            label: 'Forgot PIN?',
            emphasis: true,
            onPressed: state.busy
                ? null
                : () => Navigator.of(context).push<void>(
                      MaterialPageRoute<void>(
                        builder: (_) => const PinResetScreen(),
                      ),
                    ),
          ),
        ),
        const SizedBox(height: GpSpace.sm),
        GpPrimaryButton(
          key: const Key('pinSignIn.submit'),
          label: 'Sign in',
          busyLabel: 'Checking…',
          busy: state.busy,
          onPressed: _submit,
        ),
        AuthMessage(
          message: state.message,
          correlationId: state.correlationId,
        ),
      ],
    );
  }

  void _submit() => ref
      .read(pinSignInControllerProvider.notifier)
      .submitCredentials(_number.text, _pin.text);
}

class _CodePane extends ConsumerStatefulWidget {
  const _CodePane();

  @override
  ConsumerState<_CodePane> createState() => _CodePaneState();
}

class _CodePaneState extends ConsumerState<_CodePane> {
  String _code = '';
  bool _expired = false;

  @override
  Widget build(BuildContext context) {
    final PinSignInState state = ref.watch(pinSignInControllerProvider);
    final PinSignInController controller =
        ref.read(pinSignInControllerProvider.notifier);
    final OtpChallenge challenge = state.challenge!;

    return AuthScaffold(
      title: 'Enter your code',
      onBack: state.busy ? null : controller.restart,
      subtitle: Text.rich(
        TextSpan(
          style: GpTypography.bodyMedium,
          children: <InlineSpan>[
            const TextSpan(text: 'We sent a six-digit code to '),
            TextSpan(
              text: challenge.destination,
              style: GpTypography.bodyMedium.copyWith(
                color: GpPalette.ink,
                fontWeight: FontWeight.w700,
              ),
            ),
            const TextSpan(text: '.'),
          ],
        ),
      ),
      children: <Widget>[
        GpOtpField(
          key: ValueKey<String>('pinSignIn.code.${challenge.id}'),
          enabled: !state.busy && !_expired,
          hasError: state.message != null,
          onChanged: (String value) => _code = value,
          onCompleted: state.busy ? null : controller.submitCode,
        ),
        const SizedBox(height: GpSpace.md),
        GpCountdown(
          deadline: challenge.expiresAt,
          // The injected clock, not DateTime.now: a golden of a live
          // countdown would differ on every run and could never become the
          // regression gate the rest of them are.
          now: ref.watch(clockProvider),
          // Enables Resend at the moment it becomes the only useful action,
          // rather than leaving a member entering digits into a dead code.
          onExpired: () => setState(() => _expired = true),
        ),
        const SizedBox(height: GpSpace.lg),
        GpPrimaryButton(
          key: const Key('pinSignIn.verify'),
          label: 'Verify and sign in',
          busyLabel: 'Checking…',
          busy: state.busy,
          onPressed: _expired ? null : () => controller.submitCode(_code),
        ),
        AuthMessage(
          message: state.message,
          correlationId: state.correlationId,
        ),
        const SizedBox(height: GpSpace.sm),
        Center(
          child: GpTextAction(
            key: const Key('pinSignIn.resend'),
            label: 'Send a new code',
            emphasis: true,
            onPressed: state.busy
                ? null
                : () {
                    setState(() {
                      _expired = false;
                      _code = '';
                    });
                    controller.resend();
                  },
          ),
        ),
      ],
    );
  }
}
