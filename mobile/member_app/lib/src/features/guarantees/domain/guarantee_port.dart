/// Guarantor self service, as the merged backend actually offers it.
///
/// Two acts live on `develop` today: consent turns a `pledged` guarantee
/// `active`, and release withdraws the member's own still unconsented pledge.
/// Both carry the optimistic lock version and nothing else — `MemberActBody`
/// is `{version}`, with `extra="forbid"`, and there is deliberately no field
/// saying who consents. The acting party IS the credential, verified again
/// inside the transaction under the guarantee row lock. A caller asserted
/// identity is a rejected design, so the port cannot express one.
///
/// # There is no list, and that shapes everything here
///
/// No merged endpoint reveals a member their own guarantees (#41 covers it,
/// on !31). So this port can ACT on a guarantee and cannot FIND one, which
/// means the screens built on it are complete and currently unreachable. That
/// is recorded rather than papered over: the capability flag stays false, the
/// inbox renders a not yet state, and the DoD row for the inbox stays
/// unticked. When #41 merges, a list plugs into a flow that already works
/// rather than a flow that then has to be written in a hurry.
library;

import 'package:gp_api_client/gp_api_client.dart';

/// Which act is being performed.
///
/// Named for what the MEMBER is doing, not for the HTTP route: a member
/// agrees to stand behind a loan, or takes back an offer they have not yet
/// been held to.
enum GuaranteeAct {
  /// `pledged` becomes `active`. A commitment of the member's own capacity.
  consent,

  /// The member withdraws their own pledge. Only possible while it is still
  /// unconsented; once active it needs the staff paths.
  release,
}

abstract interface class GuaranteePort {
  /// Perform [act] on [guaranteeId], fenced by [version].
  ///
  /// Throws [ApiError]. Two failures matter more than the rest:
  ///
  /// * [ApiFailureKind.conflict] — the version fence rejected the write,
  ///   because the guarantee moved after the screen read it. NOT a retry.
  /// * [ApiFailureKind.forbidden] — the credential is not the guarantor's,
  ///   the link is dead, the member has exited, or the guarantee belongs to
  ///   somebody else. The server returns ONE refusal for all of those on
  ///   purpose, and the UI must not invent a distinction it was denied.
  Future<GuaranteeOut> act(
    String guaranteeId,
    GuaranteeAct act, {
    required int version,
  });
}
