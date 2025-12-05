from odoo import fields, models

class PrintTestWizard(models.TransientModel):
    _name = "print.test.wizard"
    _description = "Test Print Wizard"

    printer_id = fields.Char(string="Printer ID", default="warehouse-1", required=True)
    
    def action_print_test(self):
        """Creates a test print job."""
        zpl = """
^XA
^PW812
^LL1218
^FO50,50^ADN,36,20^FDTEST LABEL^FS
^FO50,100^ADN,36,20^FDPrinter: {printer}^FS
^FO50,150^ADN,36,20^FDSize: 4x6 inch^FS
^FO50,250^B3N,N,100,Y,N
^FDTEST-12345^FS
^FO50,400^GB700,5,3^FS
^FO50,450^ADN,36,20^FDIf you can read this,^FS
^FO50,500^ADN,36,20^FDthe printer is working!^FS
^XZ
        """.format(printer=self.printer_id).strip()

        self.env["print.job"].create({
            "job_type": "label",
            "printer_id": self.printer_id,
            "zpl_data": zpl,
            "state": "pending"
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Test Job Sent',
                'message': 'A test label has been added to the print queue.',
                'type': 'success',
                'sticky': False,
            }
        }
