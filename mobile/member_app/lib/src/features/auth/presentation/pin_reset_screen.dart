/// Forgot PIN: member number, code, new PIN, done.
///
/// # This one has to hedge again
///
/// Sign in can name the destination because the PIN has already been
/// verified. Reset cannot verify anything first — that is the whole point of
/// it — so it is back where the OTP-only flow is: the response must be
/// identical whether or not the member number exists, the destination is not
/// returned, and the copy says "if that member number is registered".
///
/// It is the same rule applied twice with opposite results, which is worth
/// noticing: what a screen may say depends on what has been proven by the
/// time it says it.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../../../core/providers.dart';
import '../domain/pin_reset_controller.dart';
import 'auth_scaffold.dart';

class PinResetScreen extends ConsumerWidget {
  const PinResetScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final PinResetState state = ref.watch(pinResetControllerProvider);
    return switch (state.step) {
      PinResetStep.number => const _NumberPane(),
      PinResetStep.code => const _CodePane(),
      PinResetStep.newPin => const _NewPinPane(),
      PinResetStep.done => const _DonePane(),
    };
  }
}

class _NumberPane extends ConsumerStatefulWidget {
  const _NumberPane();

  @override
  ConsumerState<_NumberPane> createState() => _NumberPaneState();
}

class _NumberPaneState extends ConsumerState<_NumberPane> {
  final TextEditingController _number = TextEditingController();

  @override
  void dispose() {
    _number.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final PinResetState state = ref.watch(pinResetControllerProvider);
    return AuthScaffold(
      title: 'Forgot your PIN?',
      onBack: state.busy ? null : () => Navigator.of(context).maybePop(),
      subtitle: const Text(
        'Enter your member number and we will help you set a new one.',
        style: GpTypography.bodyMedium,
      ),
      children: <Widget>[
        GpField(
          key: const Key('pinReset.number'),
          label: 'Member number',
          controller: _number,
          hint: 'As on your passbook',
          enabled: !state.busy,
          textInputAction: TextInputAction.go,
          onSubmitted: state.busy ? null : (_) => _submit(),
        ),
        const SizedBox(height: GpSpace.lg),
        const GpBanner(
          'We will send a code to the phone number or email address '
          'registered on your account.',
          icon: Icons.shield_outlined,
        ),
        const SizedBox(height: GpSpace.lg),
        GpPrimaryButton(
          key: const Key('pinReset.submit'),
          label: 'Continue',
          busyLabel: 'Sending…',
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

  void _submit() =>
      ref.read(pinResetControllerProvider.notifier).submitNumber(_number.text);
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
    final PinResetState state = ref.watch(pinResetControllerProvider);
    final PinResetController controller =
        ref.read(pinResetControllerProvider.notifier);

    return AuthScaffold(
      title: 'Check your messages',
      onBack: state.busy ? null : controller.restart,
      // No destination here, deliberately: naming it would confirm the member
      // number is real, and nothing has been proven at this point.
      subtitle: const Text(
        'If that member number is registered, a six-digit code is on its '
        'way to the phone or email on the account.',
        style: GpTypography.bodyMedium,
      ),
      children: <Widget>[
        GpOtpField(
          key: const Key('pinReset.code'),
          enabled: !state.busy,
          hasError: state.message != null,
          onChanged: (String value) => _code = value,
          onCompleted: state.busy ? null : controller.submitCode,
        ),
        if (state.challenge != null) ...<Widget>[
          const SizedBox(height: GpSpace.md),
          GpCountdown(
            deadline: state.challenge!.expiresAt,
            now: ref.watch(clockProvider),
          ),
        ],
        const SizedBox(height: GpSpace.lg),
        GpPrimaryButton(
          key: const Key('pinReset.verify'),
          label: 'Verify code',
          busyLabel: 'Checking…',
          busy: state.busy,
          onPressed: () => controller.submitCode(_code),
        ),
        AuthMessage(
          message: state.message,
          correlationId: state.correlationId,
        ),
      ],
    );
  }
}

class _NewPinPane extends ConsumerStatefulWidget {
  const _NewPinPane();

  @override
  ConsumerState<_NewPinPane> createState() => _NewPinPaneState();
}

class _NewPinPaneState extends ConsumerState<_NewPinPane> {
  final TextEditingController _pin = TextEditingController();
  final TextEditingController _confirm = TextEditingController();

  @override
  void dispose() {
    _pin.dispose();
    _confirm.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final PinResetState state = ref.watch(pinResetControllerProvider);
    final int length = ref.watch(flavorProvider).pinLength;

    return AuthScaffold(
      title: 'Create a new PIN',
      subtitle: Text(
        'Choose $length digits you will remember.',
        style: GpTypography.bodyMedium,
      ),
      children: <Widget>[
        GpField(
          key: const Key('pinReset.newPin'),
          label: 'New PIN',
          controller: _pin,
          enabled: !state.busy,
          obscure: true,
          keyboardType: TextInputType.number,
          inputFormatters: <TextInputFormatter>[
            FilteringTextInputFormatter.digitsOnly,
            LengthLimitingTextInputFormatter(length),
          ],
        ),
        const SizedBox(height: GpSpace.md),
        // The rules as chips, stated up front rather than as an error after
        // the fact. A member who is told "not sequential" only once they have
        // typed 1234 twice has been made to fail for no reason.
        Wrap(
          spacing: GpSpace.sm,
          runSpacing: GpSpace.sm,
          children: <Widget>[
            GpPill('$length digits'),
            const GpPill('Not all the same'),
            const GpPill('Not in sequence'),
          ],
        ),
        const SizedBox(height: GpSpace.lg),
        GpField(
          key: const Key('pinReset.confirmPin'),
          label: 'Confirm new PIN',
          controller: _confirm,
          enabled: !state.busy,
          obscure: true,
          keyboardType: TextInputType.number,
          textInputAction: TextInputAction.go,
          inputFormatters: <TextInputFormatter>[
            FilteringTextInputFormatter.digitsOnly,
            LengthLimitingTextInputFormatter(length),
          ],
          onSubmitted: state.busy ? null : (_) => _submit(),
        ),
        const SizedBox(height: GpSpace.xl),
        GpPrimaryButton(
          key: const Key('pinReset.setPin'),
          label: 'Set new PIN',
          busyLabel: 'Saving…',
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
      .read(pinResetControllerProvider.notifier)
      .submitNewPin(_pin.text, _confirm.text);
}

class _DonePane extends ConsumerWidget {
  const _DonePane();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return AuthScaffold(
      title: 'Your PIN is set',
      subtitle: const Text(
        'Use your new PIN the next time you sign in.',
        style: GpTypography.bodyMedium,
      ),
      children: <Widget>[
        const GpBanner(
          'Never share your PIN or a verification code with anyone, '
          'including people who say they are from your SACCO.',
          tone: GpBannerTone.positive,
          icon: Icons.verified_user_rounded,
        ),
        const SizedBox(height: GpSpace.xl),
        GpPrimaryButton(
          key: const Key('pinReset.done'),
          label: 'Back to sign in',
          onPressed: () {
            ref.read(pinResetControllerProvider.notifier).restart();
            Navigator.of(context).maybePop();
          },
        ),
      ],
    );
  }
}
