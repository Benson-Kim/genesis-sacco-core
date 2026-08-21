/// The consent / release flow.
///
/// Small, because the act is small: one confirmation, one call, one outcome.
/// What earns the file is the 409, which is the only place in the member app
/// where the honest answer is "we cannot fix this for you here".
///
/// A stale version means the guarantee moved after this screen read it —
/// somebody else acted, or staff did, or the application advanced a stage.
/// The usual remedy is refetch and re present. There is no member facing GET
/// for a guarantee (#41), so the app cannot refetch, and a retry button would
/// send the same dead version again and fail identically. Saying so plainly,
/// once, is better than a control that cannot work.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:meta/meta.dart';

import '../../../core/providers.dart';
import 'guarantee_port.dart';
import 'guarantee_view.dart';

enum GuaranteeActionStatus {
  /// Showing the guarantee, waiting for a decision.
  idle,

  /// The call is in flight. The confirm control is disabled (FM-G).
  submitting,

  /// The server accepted it. The screen shows the new status.
  done,

  /// The version fence rejected the write. Terminal for this screen.
  stale,

  /// Anything else. [GuaranteeActionState.message] says what to do.
  failed,
}

@immutable
class GuaranteeActionState {
  const GuaranteeActionState({
    this.status = GuaranteeActionStatus.idle,
    this.result,
    this.message,
    this.correlationId,
  });

  final GuaranteeActionStatus status;

  /// The server's word on the row after a successful act, including its new
  /// status. Rendered rather than assumed: the client does not guess that
  /// consent produced `active`. Already translated out of the wire type, so
  /// the sheet never touches a transport shape.
  final GuaranteeView? result;

  final String? message;
  final String? correlationId;

  bool get busy => status == GuaranteeActionStatus.submitting;
}

class GuaranteeActionController extends Notifier<GuaranteeActionState> {
  late GuaranteePort _guarantees;

  @override
  GuaranteeActionState build() {
    _guarantees = ref.watch(guaranteePortProvider);
    return const GuaranteeActionState();
  }

  /// Perform [act] on [guaranteeId] at [version].
  ///
  /// Callers show a confirmation first. This does not confirm anything: a
  /// controller that opened dialogs would be a controller that cannot be
  /// tested without a widget tree.
  Future<void> submit(
    String guaranteeId,
    GuaranteeAct act, {
    required int version,
  }) async {
    if (state.busy) {
      return;
    }
    state = const GuaranteeActionState(
      status: GuaranteeActionStatus.submitting,
    );
    final GuaranteeOut record;
    try {
      record = await _guarantees.act(guaranteeId, act, version: version);
    } on ApiError catch (error) {
      state = _failure(error, act);
      return;
    }
    state = GuaranteeActionState(
      status: GuaranteeActionStatus.done,
      result: GuaranteeView.of(record),
    );
  }

  GuaranteeActionState _failure(ApiError error, GuaranteeAct act) {
    switch (error.kind) {
      case ApiFailureKind.conflict:
        return GuaranteeActionState(
          status: GuaranteeActionStatus.stale,
          message: 'This request changed while it was open, so it was not '
              'submitted. Close this and open it again to see where it '
              'stands now.',
          correlationId: error.correlationId,
        );
      case ApiFailureKind.forbidden:
        // The server answers ONE refusal for a dead link, an exited member,
        // somebody else's guarantee and an already consented one. Repeating
        // that single meaning is the whole job here; guessing which of them
        // it was would invent a disclosure the server refused to make.
        return GuaranteeActionState(
          status: GuaranteeActionStatus.failed,
          message: act == GuaranteeAct.consent
              ? 'This request can no longer be accepted.'
              : 'This pledge can no longer be withdrawn.',
          correlationId: error.correlationId,
        );
      case ApiFailureKind.transport:
        return const GuaranteeActionState(
          status: GuaranteeActionStatus.failed,
          message: 'We could not reach Genesis Prestige. Nothing was '
              'submitted. Check your connection and try again.',
        );
      case ApiFailureKind.rateLimited:
        return const GuaranteeActionState(
          status: GuaranteeActionStatus.failed,
          message: 'Too many attempts. Wait a minute, then try again.',
        );
      case ApiFailureKind.unauthenticated:
        // The session layer is already tearing down; the router will move.
        return const GuaranteeActionState(
          status: GuaranteeActionStatus.failed,
          message: 'Your session ended. Sign in again to continue.',
        );
      case ApiFailureKind.server:
      case ApiFailureKind.malformedResponse:
        return GuaranteeActionState(
          status: GuaranteeActionStatus.failed,
          message: 'Something went wrong on our side. Nothing was submitted.',
          correlationId: error.correlationId,
        );
    }
  }
}

final NotifierProvider<GuaranteeActionController, GuaranteeActionState>
    guaranteeActionControllerProvider =
    NotifierProvider<GuaranteeActionController, GuaranteeActionState>(
  GuaranteeActionController.new,
);
