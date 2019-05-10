from svod_rcgn.tools.print import print_fun


class NotifyPrint:

    @staticmethod
    def notify(name, position=None, company=None, image=None):
        msg_strings = ['%s has been detected' % name]
        if position:
            msg_strings.append('Position: %s' % position)
        if company:
            msg_strings.append('Company: %s' % company)
        if image is not None:
            msg_strings.append('[IMAGE]')
        sl = max([len(s) for s in msg_strings])
        print_fun("=" * sl)
        print_fun('\r\n'.join(msg_strings))
        print_fun("=" * sl)