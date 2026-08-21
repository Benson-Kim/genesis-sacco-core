/// Deciding on a guarantee request.
///
/// Standing behind someone else's loan is the largest commitment this app
/// asks a member to make, and it is irreversible through this app once made:
/// consent turns the pledge active, and an active guarantee can only be
/// released through the staff paths. So the flow is two steps rather than
/// one, and the second step states the consequence in plain words rather than
/// asking "are you sure".
///
/// # What the merged contract does not give this screen
///
/// `GuaranteeOut` carries `borrower_member_id`, and no name. There is nothing
/// here to render as "you would be guaranteeing Amina" — only a UUID, which
/// helps nobody and would be the least useful thing on the screen. So the
/// borrower is shown as a short reference for support to trace, the figure
/// and the status carry the meaning, and the gap is recorded rather than
/// filled with a guess. It is worth a backend work item; it is not worth a
/// client side invention.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_ui/gp_ui.dart';

import '../domain/guarantee_action_controller.dart';
import '../domain/guarantee_port.dart';
import '../domain/guarantee_view.dart';

/// Standing maps to a pill tone HERE rather than on [GuaranteeView], so the
/// domain stays free of design system types. The mapping is the only thing
/// the presentation layer decides about status.
GpPillTone _toneFor(GuaranteeStanding standing) {
  switch (standing) {
    case GuaranteeStanding.awaitingDecision:
      return GpPillTone.brand;
    case GuaranteeStanding.active:
      return GpPillTone.positive;
    case GuaranteeStanding.withdrawn:
      return GpPillTone.neutral;
    case GuaranteeStanding.rejected:
      return GpPillTone.danger;
    case GuaranteeStanding.unknown:
      return GpPillTone.neutral;
  }
}

/// The sheet, driven by [GuaranteeActionController].
class GuaranteeRequestSheet extends ConsumerStatefulWidget {
  const GuaranteeRequestSheet({required this.request, super.key});

  final GuaranteeView request;

  @override
  ConsumerState<GuaranteeRequestSheet> createState() =>
      _GuaranteeRequestSheetState();
}

class _GuaranteeRequestSheetState
    extends ConsumerState<GuaranteeRequestSheet> {
  /// Which act the member has chosen but not yet confirmed.
  GuaranteeAct? _pending;

  @override
  Widget build(BuildContext context) {
    final GuaranteeActionState action =
        ref.watch(guaranteeActionControllerProvider);

    return Padding(
      padding: const EdgeInsets.all(GpSpace.xl),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          const _Grabber(),
          const SizedBox(height: GpSpace.xl),
          _Summary(request: widget.request),
          const SizedBox(height: GpSpace.xl),
          switch (action.status) {
            GuaranteeActionStatus.done => _Outcome(action: action),
            GuaranteeActionStatus.stale => _Terminal(
                icon: Icons.update_rounded,
                tone: GpBannerTone.warning,
                action: action,
              ),
            GuaranteeActionStatus.failed => _Terminal(
                icon: Icons.error_outline_rounded,
                tone: GpBannerTone.danger,
                action: action,
              ),
            GuaranteeActionStatus.idle ||
            GuaranteeActionStatus.submitting =>
              _pending == null
                  ? _Choices(onChoose: (GuaranteeAct a) => setState(() {
                        _pending = a;
                      }))
                  : _Confirm(
                      act: _pending!,
                      request: widget.request,
                      busy: action.busy,
                      onBack: action.busy
                          ? null
                          : () => setState(() => _pending = null),
                      onConfirm: () => ref
                          .read(guaranteeActionControllerProvider.notifier)
                          .submit(
                            widget.request.id,
                            _pending!,
                            version: widget.request.version,
                          ),
                    ),
          },
        ],
      ),
    );
  }
}

class _Grabber extends StatelessWidget {
  const _Grabber();

  @override
  Widget build(BuildContext context) => Center(
        child: Container(
          width: 40,
          height: 4,
          decoration: BoxDecoration(
            color: GpPalette.line,
            borderRadius: BorderRadius.circular(GpRadius.pill),
          ),
        ),
      );
}

class _Summary extends StatelessWidget {
  const _Summary({required this.request});

  final GuaranteeView request;

  @override
  Widget build(BuildContext context) {
    return GpCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              const Expanded(
                child: Text(
                  'Guarantee request',
                  style: GpTypography.titleMedium,
                ),
              ),
              GpPill(request.statusLabel, tone: _toneFor(request.standing)),
            ],
          ),
          const SizedBox(height: GpSpace.lg),
          const Text('You would be standing behind',
              style: GpTypography.bodySmall),
          const SizedBox(height: GpSpace.xs),
          GpMoney(request.figure, size: GpMoneySize.large),
          const SizedBox(height: GpSpace.lg),
          const Divider(height: 1, color: GpPalette.line),
          const SizedBox(height: GpSpace.md),
          _Row(label: 'Reference', value: request.reference),
          const SizedBox(height: GpSpace.sm),
          _Row(label: 'Borrower', value: request.borrowerReference),
        ],
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          SizedBox(
            width: 92,
            child: Text(label, style: GpTypography.bodySmall),
          ),
          Expanded(
            child: Text(
              value,
              style: GpTypography.labelSmall.copyWith(color: GpPalette.ink),
            ),
          ),
        ],
      );
}

class _Choices extends StatelessWidget {
  const _Choices({required this.onChoose});

  final ValueChanged<GuaranteeAct> onChoose;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        GpPrimaryButton(
          label: 'Agree to guarantee',
          onPressed: () => onChoose(GuaranteeAct.consent),
        ),
        const SizedBox(height: GpSpace.sm),
        GpSecondaryButton(
          label: 'Withdraw my pledge',
          onPressed: () => onChoose(GuaranteeAct.release),
        ),
      ],
    );
  }
}

class _Confirm extends StatelessWidget {
  const _Confirm({
    required this.act,
    required this.request,
    required this.busy,
    required this.onBack,
    required this.onConfirm,
  });

  final GuaranteeAct act;
  final GuaranteeView request;
  final bool busy;
  final VoidCallback? onBack;
  final VoidCallback onConfirm;

  @override
  Widget build(BuildContext context) {
    final bool consenting = act == GuaranteeAct.consent;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        GpBanner(
          consenting
              // States the consequence, and states it as a consequence. This
              // app cannot undo it afterwards, so saying so before is the
              // only honest place to say it.
              ? 'If the borrower does not repay, your savings can be used '
                  'towards this. You cannot withdraw a guarantee from this '
                  'app once you have agreed to it.'
              : 'Withdrawing removes your pledge from this request. The '
                  'borrower may need to find another guarantor.',
          tone: consenting ? GpBannerTone.warning : GpBannerTone.info,
          icon: consenting
              ? Icons.warning_amber_rounded
              : Icons.info_outline_rounded,
        ),
        const SizedBox(height: GpSpace.lg),
        GpPrimaryButton(
          label: consenting
              ? 'Yes, guarantee this'
              : 'Yes, withdraw my pledge',
          busyLabel: 'Submitting…',
          busy: busy,
          onPressed: onConfirm,
        ),
        const SizedBox(height: GpSpace.sm),
        GpSecondaryButton(label: 'Go back', onPressed: onBack),
      ],
    );
  }
}

class _Outcome extends StatelessWidget {
  const _Outcome({required this.action});

  final GuaranteeActionState action;

  @override
  Widget build(BuildContext context) {
    // The server's word on the row, not an assumption about what the act
    // produced. The client never guesses that consent yields `active`.
    final GuaranteeView after = action.result!;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        GpBanner(
          'Recorded. This request is now ${after.statusLabel.toLowerCase()}.',
          tone: GpBannerTone.positive,
          icon: Icons.check_circle_rounded,
        ),
        const SizedBox(height: GpSpace.lg),
        GpSecondaryButton(
          label: 'Done',
          onPressed: () => Navigator.of(context).maybePop(),
        ),
      ],
    );
  }
}

class _Terminal extends StatelessWidget {
  const _Terminal({
    required this.icon,
    required this.tone,
    required this.action,
  });

  final IconData icon;
  final GpBannerTone tone;
  final GuaranteeActionState action;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        GpBanner(action.message ?? '', tone: tone, icon: icon),
        if (action.correlationId != null) ...<Widget>[
          const SizedBox(height: GpSpace.sm),
          Text(
            'Ref: ${action.correlationId}',
            style: GpTypography.bodySmall,
            textAlign: TextAlign.center,
          ),
        ],
        const SizedBox(height: GpSpace.lg),
        // No retry. A stale version would send the same dead value again and
        // fail identically, and there is no member facing GET to refetch a
        // fresh one with (#41). A control that cannot work is worse than none.
        GpSecondaryButton(
          label: 'Close',
          onPressed: () => Navigator.of(context).maybePop(),
        ),
      ],
    );
  }
}
