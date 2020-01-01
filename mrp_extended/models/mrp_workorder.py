# -*- coding: utf-8 -*-
# Part of Odoo. See COPYRIGHT & LICENSE files for full copyright and licensing details.

from datetime import datetime, timedelta
from odoo.tools import float_compare, float_round
from odoo import models, fields, api, _
from odoo.exceptions import UserError, Warning


class MrpWorkorder(models.Model):
    _inherit = "mrp.workorder"

    is_reworkorder = fields.Boolean("Is Rework Order",
                                     help="Check if workorder is rework order or not.")
    reworkorder_id = fields.Many2one("mrp.workorder", "Rework Station Workorder",
                                                  help="Workorder in rework station to check rework process is done.")
    to_reworkorder_line_ids = fields.One2many('mrp.workorder.line',
        'orig_rewo_id', string='To Rework')
    finished_reworkorder_line_ids = fields.One2many('mrp.workorder.line',
        'finished_reworkorder_id', string='finished Re-Workorder Lines')

    has_rework = fields.Boolean(compute='_check_has_rework', store=True)

    @api.depends('current_quality_check_id', 'to_reworkorder_line_ids', 'to_reworkorder_line_ids.move_line_id', 'lot_id')
    def _check_has_rework(self):
        for wo in self:
            if wo.current_quality_check_id.workorder_line_id.move_line_id.id in wo.to_reworkorder_line_ids.mapped('move_line_id').ids:
                wo.has_rework = True
            else:
                wo.has_rework = False

    @api.depends('production_id.workorder_ids')
    def _compute_is_last_unfinished_wo(self):
        for wo in self:
            if not wo.is_reworkorder:
                other_wos = wo.production_id.workorder_ids.filtered(lambda wokorder: not wokorder.is_reworkorder) - wo
                other_states = other_wos.mapped(lambda w: w.state == 'done')
                wo.is_last_unfinished_wo = all(other_states)
            else:
                wo.is_last_unfinished_wo = False

    @api.depends('qty_production', 'qty_produced')
    def _compute_qty_remaining(self):
        for wo in self.filtered(lambda workorder: not workorder.is_reworkorder):
            wo.qty_remaining = float_round(wo.qty_production - wo.qty_produced, precision_rounding=wo.production_id.product_uom_id.rounding)
        for rewo in self.filtered(lambda reworkorder: reworkorder.is_reworkorder):
            reworkorder_lines = rewo._defaults_from_to_reworkorder_line()
            qty_to_consume = len(reworkorder_lines.filtered(lambda rewol: rewol.move_line_id))
            rewo.qty_remaining = float_round(qty_to_consume or 1, precision_rounding=rewo.production_id.product_uom_id.rounding)

    sta = fields.Selection([("pass", "Pass"),
                            ("fail", "Fail")],
                           string="STA", help="STA Test Result")
    crp = fields.Selection([("pass", "Pass"),
                            ("fail", "Fail")],
                           string="CRP", help="CRP Test Result")
    votage_test = fields.Selection([("pass", "Pass"),
                                    ("fail", "Fail")],
                                   string="Votage Test", help="Votage Test Result")
    result = fields.Selection([("pass", "Pass"),
                               ("fail", "Fail")],
                              string="Result", help="Test Result")
    last_test_import_date = fields.Date("Last Test Import Date")
    last_test_import_user = fields.Many2one("res.users", "Last Test Import User")
    orig_move_line_id = fields.Many2one('stock.move.line')

    @api.model
    def _search(self, args, offset=0, limit=None, order=None, count=False, access_rights_uid=None):
        context = self._context or {}
        if context.get('reworkorder_id', False):
            for index in range(len(args)):
                if args[index][0] == "next_work_order_id" and isinstance(args[index][2], int) and args[index][2] == context['reworkorder_id']:
                    args[index] = ("reworkorder_id", args[index][1], args[index][2])
        return super(MrpWorkorder, self)._search(args, offset, limit, order, count=count, access_rights_uid=access_rights_uid)

    def _defaults_from_to_reworkorder_line(self):
        self.ensure_one()
        to_reworkorder_line_ids = self.env['mrp.workorder.line']
        origin_wo_ids = self.env[self._name].search([
                ('reworkorder_id', '=', self.id)
            ])
        for wo in origin_wo_ids:
            to_reworkorder_line_ids |= wo.to_reworkorder_line_ids
        return to_reworkorder_line_ids

    def _defaults_from_finished_workorder_line(self, reference_lot_lines):
        if self.is_reworkorder:
            reference_lot_lines = reference_lot_lines.filtered(lambda rll: not rll.finished_workorder_id)
            for rework_line in self._defaults_from_to_reworkorder_line().filtered(lambda rewol: rewol.move_line_id):
                reference_lot_lines |= rework_line
        for r_line in reference_lot_lines:
            # see which lot we could suggest and its related qty_producing
            if not r_line.lot_id:
                continue
            candidates = self.finished_workorder_line_ids.filtered(lambda line: line.lot_id == r_line.lot_id)
            rounding = self.product_uom_id.rounding
            if not candidates:
                self.write({
                    'finished_lot_id': r_line.lot_id.id,
                    'qty_producing': r_line.qty_done,
                    'orig_move_line_id': r_line.move_line_id.id,
                })
                return True
            elif float_compare(candidates.qty_done, r_line.qty_done, precision_rounding=rounding) < 0:
                self.write({
                    'finished_lot_id': r_line.lot_id.id,
                    'qty_producing': r_line.qty_done - candidates.qty_done,
                    'orig_move_line_id': r_line.move_line_id.id,
                })
                return True
            elif self.is_reworkorder:
                self.write({
                    'finished_lot_id': r_line.lot_id.id,
                    'qty_producing': r_line.qty_done,
                    'orig_move_line_id': r_line.move_line_id.id,
                })
                return True
        return False

    def _apply_update_workorder_lines(self):
        previous_wo = self.env[self._name].search([
                    ('next_work_order_id', '=', self.id)
                ])
        if previous_wo and previous_wo.reworkorder_id:
            return super(MrpWorkorder, previous_wo.reworkorder_id)._apply_update_workorder_lines()
        return super(MrpWorkorder, self)._apply_update_workorder_lines()

    @api.model
    def _generate_lines_values(self, move, qty_to_consume):
        """ Create workorder line. First generate line based on the reservation,
        in order to prefill reserved quantity, lot and serial number.
        If the quantity to consume is greater than the reservation quantity then
        create line with the correct quantity to consume but without lot or
        serial number.
        """
        lines = []
        is_tracked = move.product_id.tracking != 'none'
        if move in self.move_raw_ids._origin:
            # Get the inverse_name (many2one on line) of raw_workorder_line_ids
            initial_line_values = {self.raw_workorder_line_ids._get_raw_workorder_inverse_name(): self.id}
        else:
            # Get the inverse_name (many2one on line) of finished_workorder_line_ids
            initial_line_values = {self.finished_workorder_line_ids._get_finished_workoder_inverse_name(): self.id}

        # # finished and not in reworkorder
        # move_line_ids = move.move_line_ids.filtered(lambda ml: ml.lot_id and ml.lot_id.id not in self.to_reworkorder_line_ids.mapped('move_line_id').mapped('lot_id').ids)
        # if not move_line_ids and self.to_reworkorder_line_ids:
        #     move_line_ids = self.to_reworkorder_line_ids.mapped('move_line_id')

        move_line_ids = ((move.move_line_ids - move.move_line_ids.filtered(lambda ml: ml.lot_produced_ids or float_compare(ml.product_uom_qty, ml.qty_done, precision_rounding=move.product_uom.rounding) <= 0)) - self.to_reworkorder_line_ids.mapped('move_line_id'))
        if not move_line_ids:
            move_line_ids = self.to_reworkorder_line_ids.mapped('move_line_id')
        for move_line in move_line_ids:
            line = dict(initial_line_values)
            if float_compare(qty_to_consume, 0.0, precision_rounding=move.product_uom.rounding) <= 0:
                break
            # move line already 'used' in workorder (from its lot for instance)
            if move_line.lot_produced_ids or float_compare(move_line.product_uom_qty, move_line.qty_done, precision_rounding=move.product_uom.rounding) <= 0:
                continue
            # search wo line on which the lot is not fully consumed or other reserved lot
            linked_wo_line = self._workorder_line_ids().filtered(
                lambda line: line.move_id == move and
                line.lot_id == move_line.lot_id
            )
            if linked_wo_line:
                if float_compare(sum(linked_wo_line.mapped('qty_to_consume')), move_line.product_uom_qty - move_line.qty_done, precision_rounding=move.product_uom.rounding) < 0:
                    to_consume_in_line = min(qty_to_consume, move_line.product_uom_qty - move_line.qty_done - sum(linked_wo_line.mapped('qty_to_consume')))
                else:
                    continue
            else:
                to_consume_in_line = min(qty_to_consume, move_line.product_uom_qty - move_line.qty_done)
            line.update({
                'move_id': move.id,
                'product_id': move.product_id.id,
                'product_uom_id': is_tracked and move.product_id.uom_id.id or move.product_uom.id,
                'qty_to_consume': to_consume_in_line,
                'qty_reserved': to_consume_in_line,
                'lot_id': move_line.lot_id.id,
                'move_line_id': move_line.id,
                'qty_done': to_consume_in_line,
            })
            lines.append(line)
            qty_to_consume -= to_consume_in_line
        # The move has not reserved the whole quantity so we create new wo lines
        if float_compare(qty_to_consume, 0.0, precision_rounding=move.product_uom.rounding) > 0:
            line = dict(initial_line_values)
            if move.product_id.tracking == 'serial':
                while float_compare(qty_to_consume, 0.0, precision_rounding=move.product_uom.rounding) > 0:
                    line.update({
                        'move_id': move.id,
                        'product_id': move.product_id.id,
                        'product_uom_id': move.product_id.uom_id.id,
                        'qty_to_consume': 1,
                        'qty_done': 1,
                    })
                    lines.append(line)
                    qty_to_consume -= 1
            else:
                line.update({
                    'move_id': move.id,
                    'product_id': move.product_id.id,
                    'product_uom_id': move.product_uom.id,
                    'qty_to_consume': qty_to_consume,
                    'qty_done': qty_to_consume,
                })
                lines.append(line)
        steps = self._get_quality_points(lines)
        for line in lines:
            if line['product_id'] in steps.mapped('component_id.id') or move.has_tracking != 'none':
                line['qty_done'] = 0
        return lines

    def do_rework(self):
        self.ensure_one()
        mrp_rework_orders_action = self.env["ir.config_parameter"].sudo().get_param("mrp_extended.mrp_rework_orders_action")
        default_reworkcenter_id = self.env["ir.config_parameter"].sudo().get_param("mrp_extended.default_reworkcenter_id")
        self.reworkorder_id = self.production_id.workorder_ids.filtered(
                lambda wo: wo.workcenter_id.id == int(default_reworkcenter_id) and wo.state not in ('cancel', 'done') and wo.is_reworkorder
            )
        if mrp_rework_orders_action == "manual" and not self.reworkorder_id:
            self.reworkorder_id = self.production_id._create_reworkorder()

        self.reworkorder_id.with_context(force_date=True).write({
            'date_planned_start': self.date_planned_start,
            'date_planned_finished': self.date_planned_finished,
            'state': 'ready',
        })

        check_id = self.current_quality_check_id
        self._assign_component_lot_to_finish_lot()
        self.raw_workorder_line_ids = [(1, line.id, {
                'raw_workorder_id': False,
                'orig_rewo_id': self.id,
                'qty_done': 1,
                'company_id': line._get_production().company_id.id,
                'lot_id': self.finished_lot_id,
            }) for line in self.raw_workorder_line_ids]

        check_id.unlink()

        reworkorder_lines = self.reworkorder_id._defaults_from_to_reworkorder_line().filtered(lambda rewol: rewol.move_line_id)
        # reference_lot_lines = self.to_reworkorder_line_ids.filtered(lambda rewol: rewol.move_line_id)
        self.reworkorder_id._defaults_from_finished_workorder_line(reworkorder_lines.sorted(key=lambda rewol: rewol.create_date, reverse=True))
        self.reworkorder_id._create_checks()
        self._apply_update_workorder_lines()
        self.finished_lot_id = False
        self._create_checks()
        return True

    def _create_or_update_rework_finished_line(self):
        current_lot_lines = self.finished_reworkorder_line_ids.filtered(lambda line: line.lot_id == self.finished_lot_id)
        if not current_lot_lines:
            self.env['mrp.workorder.line'].create({
                'finished_reworkorder_id': self.id,
                'product_id': self.product_id.id,
                'product_uom_id': self.product_id.uom_id.id,
                'lot_id': self.finished_lot_id.id,
                'qty_done': self.qty_producing,
                'company_id': self.production_id.company_id.id,
            })
        else:
            current_lot_lines.qty_done += self.qty_producing

    # def _defaults_from_workorder_lines(self, move, test_type):
    #     line = super(MrpWorkorder, self)._defaults_from_workorder_lines(move, test_type)
    #     if line.get('lot_id', False):
    #         line.pop('lot_id')
    #     return line

    def record_rework_production(self):
        if not self:
            return True

        self.ensure_one()
        prev_orig_move_line_id = self.orig_move_line_id
        prev_lot_id = self.finished_lot_id
        self._check_company()
        if float_compare(self.qty_producing, 0, precision_rounding=self.product_uom_id.rounding) <= 0:
            raise UserError(_('Please set the quantity you are currently producing. It should be different from zero.'))

        # If last work order, then post lots used
        # if not self.next_work_order_id:
        #     self._update_finished_move()

        # Transfer quantities from temporary to final move line or make them final
        # self._update_moves()

        # Transfer lot (if present) and quantity produced to a finished workorder line
        if self.product_tracking != 'none':
            self._create_or_update_rework_finished_line()

        # Update workorder quantity produced
        self.qty_produced += self.qty_producing

        # Suggest a finished lot on the next workorder
        if self.next_work_order_id and self.product_tracking != 'none' and (not self.next_work_order_id.finished_lot_id or self.next_work_order_id.finished_lot_id == self.finished_lot_id):
            self.next_work_order_id._defaults_from_finished_workorder_line(self.finished_workorder_line_ids)
            # As we may have changed the quantity to produce on the next workorder,
            # make sure to update its wokorder lines
            self.next_work_order_id._apply_update_workorder_lines()

        # One a piece is produced, you can launch the next work order
        # self._start_nextworkorder()
        if prev_orig_move_line_id and prev_lot_id:
            to_reworkorder_line_ids = self.production_id.workorder_ids.mapped('to_reworkorder_line_ids')
            rewol_to_update = to_reworkorder_line_ids.filtered(lambda rewol: rewol.lot_id.id == prev_lot_id.id)
            rewol_to_update.write({
                    'move_line_id': False
                })

        # Test if the production is done
        rounding = self.production_id.product_uom_id.rounding
        # Get to rework lines length
        qty_production = len(self._defaults_from_to_reworkorder_line())
        if float_compare(self.qty_produced, qty_production, precision_rounding=rounding) < 0:
            previous_wo = self.env['mrp.workorder']
            if self.product_tracking != 'none':
                previous_wo = self.env['mrp.workorder'].search([
                    ('next_work_order_id', '=', self.id)
                ])
            candidate_found_in_previous_wo = False

            if previous_wo:
                candidate_found_in_previous_wo = self._defaults_from_finished_workorder_line(previous_wo.finished_workorder_line_ids)
            if not candidate_found_in_previous_wo:
                # self is the first workorder
                self.qty_producing = self.qty_remaining
                self.finished_lot_id = False
                if self.product_tracking == 'serial':
                    self.qty_producing = 1

            self._apply_update_workorder_lines()
        else:
            self.qty_producing = 0
            # Save reworkorder as pending
            self.button_pending()
        return True

    def record_production(self):
        finished_lot_id = self.finished_lot_id
        prev_orig_move_line_id = self.orig_move_line_id
        if self.is_reworkorder:
            self = self.with_context(reworkorder_id=self.id)
            res = self.record_rework_production()
            if not self.orig_move_line_id:
                self.button_pending()
            return res
        res = super(MrpWorkorder, self).record_production()
        # check if final WO then done rework here
        if self.is_last_unfinished_wo and self.state == "done":
            reworkorder_id = self.production_id.workorder_ids.filtered(lambda workorder: workorder.is_reworkorder)
            reworkorder_id.button_finish()
        return res

    def button_start(self):
        if self.is_reworkorder and not self.orig_move_line_id:
            raise Warning(_("No quantities available for rework."))
        return super(MrpWorkorder, self).button_start()

    def _assign_component_lot_to_finish_lot(self):
        self.ensure_one()
        if self.component_tracking != 'none' and self.lot_id and self.qty_done != 0:
            if not self.finished_lot_id:
                # Try search if available then no need to create
                candidate_lot = self.env['stock.production.lot'].search([
                        ('name', '=', self.lot_id.name),
                        ('product_id', '=', self.product_id.id),
                        ('company_id', '=', self.company_id.id),
                    ])
                if candidate_lot:
                    self.finished_lot_id = candidate_lot.id
                else:
                    self.finished_lot_id = self.env['stock.production.lot'].create({
                            'name': self.lot_id.name,
                            'product_id': self.product_id.id,
                            'company_id': self.company_id.id,
                        }).id
            else:
                self.finished_lot_id.write({"name": self.lot_id.name})
        return True

    def _next(self, continue_production=False):
        auto_record = False
        prev_quality_check_id = self.current_quality_check_id
        if self.has_rework:
            raise UserError(_("Rework of this component is currently in progress!"))
        self._assign_component_lot_to_finish_lot()
        super(MrpWorkorder, self)._next(continue_production=continue_production)
        if self.is_user_working or self.is_last_step or not self.skipped_check_ids or not self.is_last_lot:
            auto_record = True
            if prev_quality_check_id and prev_quality_check_id.point_id:
                auto_record = False
        if auto_record:
            self.record_production()

    def button_show_reworkorder(self):
        self.ensure_one()

        if not self.reworkorder_id:
            raise Warning("Rework Workorder is not available for this workorder.")

        if self.env.context.get('active_model') == self._name:
            action = self.env.ref('mrp.action_mrp_workorder_production_specific').read()[0]
            action['context'] = {'search_default_production_id': self.production_id.id}
            action['target'] = 'main'
        else:
            # workorder tablet view action should redirect to the same tablet view with same workcenter when WO mark as done.
            action = self.env.ref('mrp_workorder.mrp_workorder_action_tablet').read()[0]
            action['context'] = {
                'form_view_initial_mode': 'edit',
                'no_breadcrumbs': True,
                'search_default_workcenter_id': self.reworkorder_id.workcenter_id.id
            }
        action['domain'] = [('state', 'not in', ['done', 'cancel', 'pending'])]
        return action

    def on_barcode_scanned(self, barcode):
        # qty_done field for serial numbers is fixed
        if self.component_tracking != 'serial':
            if not self.lot_id:
                # not scanned yet
                self.qty_done = 1
            elif self.lot_id.name == barcode:
                self.qty_done += 1
            else:
                return {
                    'warning': {
                        'title': _("Warning"),
                        'message': _("You are using components from another lot. \nPlease validate the components from the first lot before using another lot.")
                    }
                }

        lot = self.env['stock.production.lot'].search([('name', '=', barcode), ('product_id', '=', self.component_id.id)])

        corresponding_rework_move_line = self.to_reworkorder_line_ids.mapped('move_line_id').filtered(
                lambda ml: lot and ml.lot_id.id == lot.id
            )

        if corresponding_rework_move_line:
            return {
                'warning': {
                    'title': _("Warning"),
                    'message': _("Corresponding product %s of barcode %s is currently in rework!\
                        You can only process once it will be processed from rework station." % (
                            corresponding_rework_move_line.product_id.display_name,
                            corresponding_rework_move_line.lot_id.name,
                        ))
                }
            }

        if self.component_tracking:
            if not lot:
                # create a new lot
                # create in an onchange is necessary here ("new" cannot work here)
                lot = self.env['stock.production.lot'].create({
                    'name': barcode,
                    'product_id': self.component_id.id,
                    'company_id': self.company_id.id,
                })
            self.lot_id = lot
        elif self.production_id.product_id.tracking and self.production_id.product_id.tracking != 'none':
            if not lot:
                lot = self.env['stock.production.lot'].create({
                    'name': barcode,
                    'product_id': self.product_id.id,
                    'company_id': self.company_id.id,
                })
            self.finished_lot_id = lot

class MrpWorkorderLine(models.Model):
    _inherit = 'mrp.workorder.line'

    orig_rewo_id = fields.Many2one('mrp.workorder', 'Origin Product for Re-Workorder',
        ondelete='cascade')
    finished_reworkorder_id = fields.Many2one('mrp.workorder', 'Origin Product for Re-Workorder',
        ondelete='cascade')
    move_line_id = fields.Many2one('stock.move.line')

    def _get_production(self):
        production_id = super(MrpWorkorderLine, self)._get_production()
        if not production_id and self.finished_reworkorder_id:
            production_id = self.finished_reworkorder_id.production_id
        return production_id
