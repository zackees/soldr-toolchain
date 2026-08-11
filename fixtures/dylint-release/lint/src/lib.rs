#![feature(rustc_private)]

extern crate rustc_errors;
extern crate rustc_hir;

use rustc_errors::DiagDecorator;
use rustc_hir::{def::Res, Expr, ExprKind};
use rustc_lint::{LateContext, LateLintPass, LintContext};

dylint_linting::declare_late_lint! {
    pub RELEASE_FIXTURE_FORBIDDEN_IO,
    Deny,
    "prove the published Dylint pair and exact-nightly driver execute together"
}

impl<'tcx> LateLintPass<'tcx> for ReleaseFixtureForbiddenIo {
    fn check_expr(&mut self, cx: &LateContext<'tcx>, expr: &'tcx Expr<'tcx>) {
        let ExprKind::Path(ref path) = expr.kind else {
            return;
        };
        let Res::Def(_, def_id) = cx.qpath_res(path, expr.hir_id) else {
            return;
        };
        let segments = cx.get_def_path(def_id);
        let is_forbidden = segments.first().is_some_and(|part| part.as_str() == "std")
            && segments.get(1).is_some_and(|part| part.as_str() == "fs")
            && segments
                .last()
                .is_some_and(|part| part.as_str() == "read_to_string");
        if is_forbidden {
            cx.opt_span_lint(
                RELEASE_FIXTURE_FORBIDDEN_IO,
                Some(expr.span),
                DiagDecorator(|diag| {
                    diag.primary_message("release fixture observed the expected forbidden I/O");
                }),
            );
        }
    }
}
