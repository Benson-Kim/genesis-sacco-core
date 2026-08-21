/// [GuaranteePort] over `POST /member/guarantees/{id}/{consent,release}`.
///
/// # One key per attempt, and what "attempt" means here
///
/// FM-G: a double tap must produce one effect, not two. The transport already
/// requires a key on every mutation; what this decides is when the key
/// ROTATES, and for an act on a versioned row the answer falls out of the
/// version itself.
///
/// The canonical string folds in the PATH as well as the body. The body is
/// `{"version":3}` and nothing else, so two different guarantees sitting at
/// version 3 would otherwise share a key. Server side that is already safe —
/// the storage key folds in the route, so a different guarantee claims a
/// different row — but a client that relies on the server to tell its own
/// requests apart is a client one refactor away from being wrong.
///
/// Rotation then does the right thing without being asked: the same act on
/// the same guarantee at the same version reuses the key, so a double tap
/// replays the stored response instead of writing twice. A retry after the
/// row moved carries a new version, rotates, and is correctly treated as a
/// new attempt rather than colliding with the old key and answering 409 for
/// reasons that have nothing to do with the guarantee.
library;

import 'dart:convert';

import 'package:gp_api_client/gp_api_client.dart';

import '../domain/guarantee_port.dart';

class MemberGuaranteeRepository implements GuaranteePort {
  MemberGuaranteeRepository(this._client);

  final GpHttpClient _client;
  final IdempotencyKeySlot _slot = IdempotencyKeySlot();

  @override
  Future<GuaranteeOut> act(
    String guaranteeId,
    GuaranteeAct act, {
    required int version,
  }) async {
    final String path = '/member/guarantees/$guaranteeId/${_segment(act)}';
    final Map<String, Object?> body = MemberActBody(version: version).toJson();
    final Map<String, Object?> response = await _client.post(
      path,
      body: body,
      idempotencyKey: _slot.keyFor('$path|${jsonEncode(body)}'),
    );
    try {
      return GuaranteeOut.fromJson(response);
    } on TypeError {
      // A 2xx of the wrong shape is a failed call, not a crash. The generated
      // fromJson casts, so a missing field would otherwise throw an uncaught
      // TypeError at the moment the member committed to something.
      throw const ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: 200,
      );
    }
  }

  static String _segment(GuaranteeAct act) {
    switch (act) {
      case GuaranteeAct.consent:
        return 'consent';
      case GuaranteeAct.release:
        return 'release';
    }
  }
}
